#!/usr/bin/python

# Copyright (c) 2014-2018 Sascha Schmidt <sascha@schmidt.ps> (author)
# Copyright (c) 2024 Matthias Urlichs <matthias@urlichs.de>
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# Error codes: http://docs.python.org/2/library/errno.html

from __future__ import with_statement

import os, sys, pwd, errno, json, argparse, traceback, dropbox
from time import time, mktime, sleep
from datetime import datetime
from stat import S_IFDIR, S_IFLNK, S_IFREG
from fuse import FUSE, FuseOSError, Operations
from errno import *
import requests
from requests.auth import HTTPBasicAuth


def space_usage_allocated(space_usage):
    '''Return the space usage allocation for an individual or team account as applicable.'''
    return space_usage.allocation.get_individual().allocated if space_usage.allocation.is_individual() else space_usage.allocation.get_team().allocated


##################################
# Class: FUSE Dropbox operations #
##################################
class Dropbox(Operations):
    def __init__(self, dbx):
        self.dbx = dbx
        self.cache = {}
        self.openfh = {}
        self.runfh = {}

    #######################################
    # Wrapper functions around API calls. #
    #######################################

    def dbxStruct(self, obj):
        structname = obj.__class__.__name__
        data = {}

        for key in dir(obj):
            if not key.startswith('_'):
                if isinstance(getattr(obj, key), list):
                    tmpdata = []
                    for item in getattr(obj, key):
                        tmpdata.append(self.dbxStruct(item))
                    data.update({key: tmpdata})
                else:
                    data.update({key: getattr(obj, key)})

        if structname == 'FolderMetadata':
            data.update({'.tag': 'folder'})
        if structname == 'FileMetadata':
            data.update({'.tag': 'file'})

        return data

    # Get Dropbox metadata of path.
    def dbxMetadata(self, path, mhash=None):
        if path == '/':
            query_path = ''
        else:
            query_path = path

        args = {'path': query_path}

        if mhash != None and mhash != 0:
            args.update({'path': mhash})
        try:
            result = dbx.files_list_folder(query_path)
            result = self.dbxStruct(result)

            if 'error' in result:
                if debug:
                    appLog('debug', 'list folder error: ' + str(result['error']))

                if result['error'] == 'not_found':
                    raise Exception('apiRequest failed. HTTPError: 404')

                if result['error'] == 'not_folder':
                    result = dbx.files_get_metadata(query_path)
                    result = self.dbxStruct(result)
                else:
                    raise Exception('API Query failed')

        except Exception as e:
            if debug:
                    appLog('debug', 'list folder exception: ' + str(e))
            return False

        result['path'] = path
        if 'entries' in result:
            for tmp in result['entries']:
                tmp['path'] = tmp['path_display']
        return result

    # Rename a Dropbox file/directory object.
    def dbxFileMove(self, old, new):
        dbx.files_move(old, new)

    # Delete a Dropbox file/directory object.
    def dbxFileDelete(self, path):
        dbx.files_delete(path)

    # Create a Dropboy folder.
    def dbxFileCreateFolder(self, path):
        dbx.files_create_folder(path)

    # Upload chunk of data to Dropbox.
    def dbxChunkedUpload(self, data, upload_id, offset=0):
        if upload_id == "":
            result = dbx.files_upload_session_start(data)
        else:
            cursor = dropbox.files.UploadSessionCursor(upload_id, offset)
            result = dbx.files_upload_session_append_v2(data, cursor)

        result = self.dbxStruct(result)
        result.update({'offset': offset+len(data), 'upload_id': result['session_id']})

        return result

    # Commit chunked upload to Dropbox.
    def dbxCommitChunkedUpload(self, path, upload_id, offset):
        cursor = dropbox.files.UploadSessionCursor(upload_id, offset)
        commitinfo = dropbox.files.CommitInfo(path)
        result = dbx.files_upload_session_finish(bytes(), cursor, commitinfo)
        result = self.dbxStruct(result)

        if debug:
            appLog('debug', 'dbxChunkedUpload: session finish result:' + str(result))
        return result

    # Get Dropbox filehandle.
    def dbxFilehandle(self, path, seek=False):
        result = dbx.files_download(path)[1].raw
        return result

    #####################
    # Helper functions. #
    #####################

    # Create data holder object.
    def createDataObj(self, path, entries):
        data = {'path': path}

        #data.update(entries)

        print("#########" + data)
        exit(-1)
        return data

    # Get a valid and unique filehandle.
    def getFH(self, mode='r'):
        for i in range(1,8193):
            if i not in self.openfh:
                self.openfh[i] = {'mode' : mode, 'f' : False, 'lock' : False, 'eoffset': 0}
                self.runfh[i] = False
                return i
        return False

    # Release a filehandle.
    def releaseFH(self, fh):
        if fh in self.openfh:
            self.openfh.pop(fh)
            self.runfh.pop(fh)
        else:
            return False

    # Remove item from cache.
    def removeFromCache(self, path):
        if debug:
            appLog('debug', 'Called removeFromCache() Path: ' + path)

        # Check whether this path exists within cache.
        if path in self.cache:
            item = self.cache[path]

            # If this is a directory, remove all childs.
            if 'entries' in item and 'contents' in item:
                # Remove folder items from cache.
                if debug:
                    appLog('debug', 'Removing childs of path from cache')
                for tmp in item['contents']:
                    if debug:
                        appLog('debug', 'Removing from cache: ' + tmp['path'])
                    if tmp['path'] in self.cache:
                        self.cache.pop(tmp['path'])
            else:
                cur_path=os.path.dirname(path)
                if cur_path in self.cache:
                    if 'entries' in self.cache[cur_path]:
                        if debug:
                            appLog('debug', 'Removing parent path from file in cache')
                        self.cache.pop(os.path.dirname(path))
            if debug:
                appLog('debug', 'Removing from cache: ' + path)
            if path in self.cache:
                self.cache.pop(path)
            return True
        else:
            if debug:
                appLog('debug', 'Path not in cache: ' + path)
            return False

    # Get metadata for a file or folder from the Dropbox API or local cache.
    def getDropboxMetadata(self, path, deep=False):
        # Metadata exists within cache.
        path_enc = path # .decode("utf-8")
        if path_enc in self.cache:
            if debug:
                appLog('debug', 'Found cached metadata for: ' + path_enc)
            item = self.cache[path_enc]

            # Check whether this is a directory and if there any remote changes.
            if 'entries' in item and item['cachets']<int(time()) or (deep and 'contents' not in item):
                # Set temporary hash value for directory non-deep cache entry.
                if debug:
                    appLog('debug', 'Metadata directory deepcheck: ' + str(deep))
                    appLog('debug', 'Cache expired for: ' + path_enc)
                if 'cachets' in item:
                        cachets = item['cachets']
                else:
                        cachets = None
                if debug:
                    appLog('debug', 'cachets: ' + str(cachets) + ' - ' + str(int(time())))
                    appLog('debug', 'Checking for changes on the remote endpoint for folder: ' + path_enc)
                try:
                    if 'id' in item:
                        hash = item['id']
                    else:
                        hash = None
                    item = self.dbxMetadata(path, hash)
                    if item.get('is_deleted', False):
                        return False
                    if debug:
                        appLog('debug', 'Remote endpoint signalizes changes. Updating local cache for folder: ' + path_enc)
                    if debug_raw:
                        appLog('debug', 'Data from Dropbox API call: metadata(' + path + ')')
                        appLog('debug', str(item))

                    # Remove outdated data from cache.
                    self.removeFromCache(path_enc)

                    # Cache new data.
                    cachets = int(time())+cache_time
                    item.update({'cachets':cachets})
                    self.cache[path_enc] = item
                    for tmp in item['entries']:
                        if 'entries' not in tmp:
                            if not tmp.get('is_deleted', False):
                                tmp.update({'cachets':cachets})
                                self.cache[tmp['path']] = tmp
                except Exception as e:
                    if debug:
                            appLog('debug', 'No remote changes detected for folder: ' + path, traceback.format_exc())
            return item
        # No cached data found, do an Dropbox API request to fetch the metadata.
        else:
            if debug:
                appLog('debug', "cache: " + str(self.cache) + " path: " + str(path_enc))
                appLog('debug', 'No cached metadata for: ' + path)
            try:
                # If the path already exists, this path (file/dir) does not exist.
                if os.path.dirname(path) in self.cache and 'contents' in self.cache[os.path.dirname(path)]:
                    if debug:
                        appLog('debug', 'Basepath exists in cache for: ' + path)
                    return False

                item = self.dbxMetadata(path)
                if not item or item.get('is_deleted', False):
                    return False
                if debug_raw:
                    appLog('debug', 'Data from Dropbox API call: metadata(' + path + ')')
                    appLog('debug', str(item))
            except Exception as e:
                if str(e) == 'apiRequest failed. HTTPError: 404':
                    return False
                else:
                    appLog('error', 'Could not fetch metadata for: ' + path, traceback.format_exc())
                    raise FuseOSError(EREMOTEIO)

            # Cache metadata if user wants to use the cache.
            cachets = int(time())+cache_time
            item.update({'cachets':cachets})
            self.cache[path] = item
            # Cache files if this item is a file.
            path = item['path']
            if 'entries' in item:
                for tmp in item['entries']:
                        self.cache[tmp['path']] = tmp
            return item

    #########################
    # Filesystem functions. #
    #########################
    def mkdir(self, path, mode):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: mkdir() - Path: ' + path)
        try:
            self.dbxFileCreateFolder(path)
        except Exception as e:
            appLog('error', 'Could not create folder: ' + path, traceback.format_exc())
            raise FuseOSError(EIO)

        # Remove outdated data from cache.
        self.removeFromCache(os.path.dirname(path))
        return 0

    # Remove a directory.
    def rmdir(self, path):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: rmdir() - Path: ' + path)
        try:
            self.dbxFileDelete(path)
        except Exception as e:
            appLog('error', 'Could not delete folder: ' + path, traceback.format_exc())
            raise FuseOSError(EIO)
        if debug:
            appLog('debug', 'Successfully deleted folder: ' + path)

        # Remove outdated data from cache.
        self.removeFromCache(path)
        self.removeFromCache(os.path.dirname(path))
        return 0

    # Remove a file.
    def unlink(self, path):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: unlink() - Path: ' + path)

        # Remove data from cache.
        self.removeFromCache(path)

        # Delete file.
        try:
            self.dbxFileDelete(path)
        except Exception as e:
            appLog('error', 'Could not delete file: ' + path, traceback.format_exc())
            raise FuseOSError(EIO)
        if debug:
            appLog('debug', 'Successfully deleted file: ' + path)

        return 0

    # Rename a file or directory.
    def rename(self, old, new):
        old = old.encode('utf-8')
        new = new.encode('utf-8')
        if debug:
            appLog('debug', 'Called: rename() - Old: ' + old + ' New: ' + new)
        try:
            self.dbxFileMove(old, new)
        except Exception as e:
            appLog('error', 'Could not rename object: ' + old, traceback.format_exc())
            raise FuseOSError(EIO)
        if debug:
            appLog('debug', 'Successfully renamed object: ' + old)
        if debug_raw:
            appLog('debug', str(result))

        # Remove outdated data from cache.
        self.removeFromCache(old)
        return 0

    # Read data from a remote filehandle.
    def read(self, path, length, offset, fh):
        path_enc = path.encode('utf-8')
        # Wait while this function is not threadable.
        while self.openfh[fh]['lock']:
            pass

        self.runfh[fh] = True
        if debug:
            appLog('debug', 'Called: read() - Path: ' + path + ' Length: ' + str(length) + ' Offset: ' + str(offset) + ' FH: ' + str(fh))
            appLog('debug', 'Excpected offset: ' + str(self.openfh[fh]['eoffset']))
        if fh in self.openfh:
            if not self.openfh[fh]['f']:
                try:
                    self.openfh[fh]['f'] = self.dbxFilehandle(path, offset)
                except Exception as e:
                    appLog('error', 'Could not open remote file: ' + path, traceback.format_exc())
                    raise FuseOSError(EIO)
            else:
                if debug:
                    appLog('debug', 'FH handle for reading process already opened')
                if self.openfh[fh]['eoffset'] != offset:
                    if debug:
                        appLog('debug', 'Requested offset differs from expected offset. Seeking to: ' + str(offset))
                    self.openfh[fh]['f'] = self.dbxFilehandle(path, offset)
                pass

        # Read from FH.
        rbytes = ''
        if debug:
            appLog('debug', 'File handler: ' + str(self.openfh[fh]['f']))
        try:
            rbytes = self.openfh[fh]['f'].read(length)
        except Exception as e:
            appLog('error', 'Could not read data from remotefile: ' + path, traceback.format_exc())
            raise FuseOSError(EIO)

        if debug:
            appLog('debug', 'Read bytes from remote source: ' + str(len(rbytes)))
        self.openfh[fh]['lock'] = False
        self.runfh[fh] = False
        self.openfh[fh]['eoffset'] = offset + len(rbytes)
        return rbytes

    # Write data to a filehandle.
    def write(self, path, buf, offset, fh):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: write() - Path: ' + path + ' Offset: ' + str(offset) + ' FH: ' + str(fh))
        try:
            # Check for the beginning of the file.
            if fh in self.openfh:
                if not self.openfh[fh]['f']:
                    if debug:
                        appLog('debug', 'Uploading first chunk to Dropbox...')
                    # Check if the write request exceeds the maximum buffer size.
                    if len(buf) >= write_cache or len(buf) < 4096:
                        if debug:
                            appLog('debug', 'Cache exceeds configured write_cache. Uploading...')
                        result = self.dbxChunkedUpload(buf, "", 0)
                        self.openfh[fh]['f'] = {'upload_id':result['upload_id'], 'offset':result['offset'], 'buf':''}
                    else:
                        if debug:
                            appLog('debug', 'Buffer does not exceed configured write_cache. Caching...')
                        self.openfh[fh]['f'] = {'upload_id':'', 'offset':0, 'buf':buf}
                    return len(buf)
                else:
                    if debug:
                        appLog('debug', 'Uploading another chunk to Dropbox...')
                    if len(buf)+len(self.openfh[fh]['f']['buf']) >= write_cache or len(buf) < 4096:
                        if debug:
                            appLog('debug', 'Cache exceeds configured write_cache. Uploading...')
                        result = self.dbxChunkedUpload(self.openfh[fh]['f']['buf']+buf, self.openfh[fh]['f']['upload_id'], self.openfh[fh]['f']['offset'])
                        self.openfh[fh]['f'] = {'upload_id':result['upload_id'], 'offset':result['offset'], 'buf':''}
                    else:
                        if debug:
                            appLog('debug', 'Buffer does not exceed configured write_cache. Caching...')
                        self.openfh[fh]['f'].update({'buf':self.openfh[fh]['f']['buf']+buf})
                    return len(buf)
            else:
                raise FuseOSError(EIO)
        except Exception as e:
            appLog('error', 'Could not write to remote file: ' + path, traceback.format_exc())
            raise FuseOSError(EIO)

    # Open a filehandle.
    def open(self, path, flags):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: open() - Path: ' + path + ' Flags: ' + str(flags))

        # Validate flags.
        if flags & os.O_APPEND:
            if debug:
                appLog('debug', 'O_APPEND mode not supported for open()')
            raise FuseOSError(EOPNOTSUPP)

        fh = self.getFH('r')
        if debug:
            appLog('debug', 'Returning unique filehandle: ' + str(fh))
        return fh

    # Create a file.
    def create(self, path, mode):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: create() - Path: ' + path + ' Mode: ' + str(mode))

        fh = self.getFH('w')
        if debug:
            appLog('debug', 'Returning unique filehandle: ' + str(fh))

        now = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')
        cachedfh = {'size':0, 'client_modified':now, 'path':path, '.tag':'file'}
        self.cache[path] = cachedfh

        return fh

    # Release (close) a filehandle.
    def release(self, path, fh):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: release() - Path: ' + path + ' FH: ' + str(fh))

        # Check to finish Dropbox upload.
        if type(self.openfh[fh]['f']) is dict and 'upload_id' in self.openfh[fh]['f'] and self.openfh[fh]['f']['upload_id'] != "":
            # Flush still existing data in buffer.
            if self.openfh[fh]['f']['buf'] != "":
                if debug:
                    appLog('debug', 'Flushing write buffer to Dropbox')
                result = self.dbxChunkedUpload(self.openfh[fh]['f']['buf'], self.openfh[fh]['f']['upload_id'], self.openfh[fh]['f']['offset'])
            if debug:
                appLog('debug', 'Finishing upload to Dropbox')
            result = self.dbxCommitChunkedUpload(path, self.openfh[fh]['f']['upload_id'], self.openfh[fh]['f']['offset'])

        # Remove outdated data from cache if handle was opened for writing.
        if self.openfh[fh]['mode'] == 'w':
            self.removeFromCache(os.path.dirname(path))

        self.releaseFH(fh)
        if debug:
            appLog('debug', 'Released filehandle: ' + str(fh))

        return 0

    # Truncate a file to overwrite it.
    def truncate(self, path, length, fh=None):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: truncate() - Path: ' + path + " Size: " + str(length))
        return 0

    # List the content of a directory.
    def readdir(self, path, fh):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: readdir() - Path: ' + path)

        # Fetch folder informations.
        fusefolder = ['.', '..']
        metadata = self.getDropboxMetadata(path, True)

        # Loop through the Dropbox API reply to build fuse structure.
        for item in metadata['entries']:
            # Append entry to fuse foldercontent.
            folderitem = os.path.basename(item['path'])
            fusefolder.append(folderitem)

        # Loop through the folder content.
        for item in fusefolder:
            yield item

    # Get properties for a directory or file.
    def getattr(self, path, fh=None):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: getattr() - Path: ' + path)

        # Get userid and groupid for current user.
        uid = pwd.getpwuid(os.getuid()).pw_uid
        gid = pwd.getpwuid(os.getuid()).pw_gid

        # Get current time.
        now = int(time())

        # Check wether data exists for item.
        item = self.getDropboxMetadata(path)
        if not item:
            raise FuseOSError(ENOENT)

        # Handle last modified times.
        if 'client_modified' in item:
            modified = str(item['client_modified'])
            #2012-08-11T12:41:30Z
            if modified.endswith('Z'):
                modified = mktime(datetime.strptime(modified, '%Y-%m-%dT%H:%M:%SZ').timetuple())
            else:
                modified = mktime(datetime.strptime(modified, '%Y-%m-%d %H:%M:%S').timetuple())
        else:
            modified = int(now)

        if debug:
            appLog('debug', "item: " + str(item))
        if 'entries' in item or item['.tag'] == 'folder':
            # Get st_nlink count for directory.
            properties = dict(
                st_mode=S_IFDIR | 0o755,
                st_size=0,
                st_ctime=modified,
                st_mtime=modified,
                st_atime=now,
                st_uid=uid,
                st_gid=gid,
                st_nlink=2
            )
            if debug:
                appLog('debug', 'Returning properties for directory: ' + path + ' (' + str(properties) + ')')
            return properties
        elif item['.tag'] == 'file':
            properties = dict(
                st_mode=S_IFREG | 0o644,
                st_size=item['size'],
                st_ctime=modified,
                st_mtime=modified,
                st_atime=now,
                st_uid=uid,
                st_gid=gid,
                st_nlink=1,
            )
            if debug:
                appLog('debug', 'Returning properties for file: ' + path + ' (' + str(properties) + ')')
            return properties

    # Flush filesystem cache. Always true in this case.
    def fsync(self, path, fdatasync, fh):
        path_enc = path.encode('utf-8')
        if debug:
            appLog('debug', 'Called: fsync() - Path: ' + path)

    # Change attributes of item. Dummy until now.
    def chmod(self, path, mode):
        if debug:
            appLog('debug', 'Called: chmod() - Path: ' + path + " Mode: " + str(mode))
        if not self.getDropboxMetadata(path):
            raise FuseOSError(ENOENT)
        return 0

    # Change modes of item. Dummy until now.
    def chattr(self, path, uid, gid):
        if debug:
            appLog('debug', 'Called: chattr() - Path: ' + path + " UID: " + str(uid) + " GID: " + str(gid))
        if not self.getDropboxMetadata(path):
            raise FuseOSError(ENOENT)
        return 0

    # Get filesystem properties.
    def statfs(self, path):
        try:
            space_usage = dbx.users_get_space_usage()
            used_space = space_usage.used*8
            allocated_space = space_usage_allocated(space_usage)*8
            free_space = allocated_space-used_space

            result = {
                'f_bsize':1024,
                'f_frsize':1,
                'f_blocks':allocated_space/1024,
                'f_bfree':free_space/1024,
                'f_bavail':free_space/1024,
                'f_namemax':255
                };
        except Exception as e:
            appLog('error', 'statfs failed:'+repr(e))
            pass

        return result

#####################
# Global functions. #
#####################

# Log messages to stdout.
def appLog(mode, text, reason=""):
    msg = "[" + mode.upper() + "] " + text
    if reason != "":
        msg = msg + " (" + reason + ")"
    print(msg)

##############
# Main entry #
##############
# Global variables.
cache_time = 120 # Seconds
write_cache = 4194304 # Bytes
use_cache = False
allow_other = False
allow_root = False
debug = False
debug_raw = False
debug_fuse = False
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', help='Show debug output', action='store_true', default=False)
    parser.add_argument('-dr', '--debug-raw', help='Show raw debug output', action='store_true', default=False)
    parser.add_argument('-df', '--debug-fuse', help='Show FUSE debug output', action='store_true', default=False)

    # New authentication
    parser.add_argument('-ak', '--app-key', help='Dropbox Application Key', default=False)
    parser.add_argument('-as', '--app-secret', help='Dropbox Application Secret', default=False)
    parser.add_argument('-ac', '--authentication-code', help='Dropbox Authentication Code', default=False)

    parser.add_argument('-ao', '--allow-other', help='Allow other users to access this FUSE filesystem', action='store_true', default=False)
    parser.add_argument('-ar', '--allow-root', help='Allow root to access this FUSE filesystem', action='store_true', default=False)
    parser.add_argument('-ct', '--cache-time', help='Cache Dropbox data for X seconds (120 by default)', default=120, type=int)
    parser.add_argument('-wc', '--write-cache', help='Cache X bytes (chunk size) before uploading to Dropbox (4 MB by default)', default=4194304, type=int)
    parser.add_argument('-bg', '--background', help='Pushes FF4D into background', action='store_false', default=True)

    parser.add_argument('mountpoint', help='Mount point for Dropbox source')
    args = parser.parse_args()

    # Set variables supplied by commandline.
    cache_time = args.cache_time
    write_cache = args.write_cache
    allow_other = args.allow_other
    allow_root = args.allow_root
    debug = args.debug
    debug_raw = args.debug_raw
    debug_fuse = args.debug_fuse

    # Check ranges and values of given arguments.
    if cache_time < 0:
        appLog('error', 'Only positive values for cache-time are possible')
        sys.exit(-1)
    if write_cache < 4096:
        appLog('error', 'The minimum write-cache has a size of 4096 Bytes')
        sys.exit(-1)

    # Check wether the mountpoint is a valid directory.
    mountpoint = args.mountpoint
    if not os.path.isdir(mountpoint):
        appLog('error', 'Given mountpoint is not a directory.')
        sys.exit(-1)

    # Check for an existing configuration file.
    tokens = {}
    try:
        scriptpath = os.path.dirname(os.path.abspath(__file__))
        f = open(scriptpath + '/ff4d.config.json', 'r')
        tokens = json.load(f)
        if debug:
            appLog('debug', 'Got tokens from configuration file: ' + tokens)
    except Exception as e:
        pass

    # Check if new credentials were given as arguments
    refresh_token = False
    if any([args.app_key, args.app_secret, args.authentication_code]):
        if not all([args.app_key, args.app_secret, args.authentication_code]):
            appLog('error', 'All of app-key, app-secret, and authentication-code are required')
            exit(-1)

        resp = requests.post(
            'https://api.dropbox.com/oauth2/token',
            auth=HTTPBasicAuth(args.app_key, args.app_secret),
            data={
                'grant_type': 'authorization_code',
                'code': args.authentication_code
            }
        )
        refresh_token = resp.json().get('refresh_token')

        if not refresh_token:
            appLog('error', 'Error getting refresh token: ' + resp.content.decode())
            exit(-1)
        
        tokens = {
            'app_key': args.app_key,
            'app_secret': args.app_secret,
            'refresh_token': refresh_token,
        }

    # Validate tokens
    dbx = dropbox.Dropbox(
        app_key=tokens['app_key'],
        app_secret=tokens['app_secret'],
        oauth2_refresh_token=tokens['refresh_token']
    )
    account_info = ''

    try:
        account_info = dbx.users_get_current_account()
        space_usage = dbx.users_get_space_usage()

    except dropbox.exceptions.AuthError as err:
        appLog('error', 'Failed to authorize against dropbox API.', traceback.format_exc())
        exit(-1)
    except Exception as e:
        appLog('error', 'Unknown error.', traceback.format_exc())
        exit(-1)

    # Save valid access token to configuration file.
    try:
        scriptpath = os.path.dirname(os.path.abspath(__file__))
        f = open(scriptpath + '/ff4d.config.json', 'w')
        json.dump(tokens, f)
        f.close()
        os.chmod(scriptpath + '/ff4d.config.json', 0o600)
        if debug:
            appLog('debug', 'Wrote tokens to configuration file.\n')
    except Exception as e:
        appLog('error', 'Could not write configuration file.', traceback.format_exc())

    # Everything went fine and we're authed against the Dropbox api.



    print("Welcome " + account_info.name.display_name)
    print("Space used: " + str(space_usage.used/1024/1024/1024) + " GB")
    print("Space available: " + str(space_usage_allocated(space_usage)/1024/1024/1024) + " GB")
    print("")
    print("Starting FUSE...")

    try:
        FUSE(Dropbox(dbx), mountpoint, foreground=args.background, debug=debug_fuse, sync_read=True, allow_other=allow_other, allow_root=allow_root)
    except Exception as e:
        appLog('error', 'Failed to start FUSE...', traceback.format_exc())
        sys.exit(-1)
