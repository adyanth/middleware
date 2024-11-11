import os
import shutil
import threading
import time

from middlewared.api import api_method
from middlewared.api.current import ReportingGeneratePasswordArgs, ReportingGeneratePasswordResult
from middlewared.service import job, pass_app, periodic, private, CallError, Service
from middlewared.utils import MIDDLEWARE_RUN_DIR
from middlewared.utils.crypto import generate_string
from passlib.apache import HtpasswdFile


BASIC_FILE = f'{MIDDLEWARE_RUN_DIR}/netdata-basic'
HTPASSWD_LOCK = threading.Lock()


class ReportingService(Service):

    @private
    async def netdataweb_basic_file(self):
        return BASIC_FILE

    @api_method(
        ReportingGeneratePasswordArgs, ReportingGeneratePasswordResult, roles=['READONLY_ADMIN'], cli_private=True
    )
    @pass_app()
    def netdataweb_generate_password(self, app):
        """
        Generate a password to access netdata web.
        That password will be stored in htpasswd format for HTTP Basic access.

        Concurrent access for the same user is not supported and may lead to undesired behavior.
        """
        # Password schema is not used here because for READONLY_ADMIN
        # will make it return "******" instead, breaking this method for that role.
        if app and app.authenticated_credentials.is_user_session:
            authenticated_user = app.authenticated_credentials.user['username']
        else:
            raise CallError('This method needs to be called from an authenticated user only.')

        if not os.path.exists(BASIC_FILE):
            with open(os.open(BASIC_FILE, flags=os.O_CREAT, mode=0o640)):
                shutil.chown(BASIC_FILE, 'root', 'www-data')

        with HTPASSWD_LOCK:
            ht = HtpasswdFile(BASIC_FILE, autosave=True, default_scheme='bcrypt')
            if ht.get_hash(authenticated_user):
                self.logger.warning('Password for %r already exists, overwriting...', authenticated_user)
            password = generate_string(16, punctuation_chars=True)
            ht.set_password(authenticated_user, password)

        try:
            expire = self.middleware.call_sync('cache.get', 'NETDATA_WEB_EXPIRE')
        except KeyError:
            expire = {}

        # Password will be valid for 8 hours
        expire[authenticated_user] = int(time.monotonic() + 60 * 60 * 8)
        self.middleware.call_sync('cache.put', 'NETDATA_WEB_EXPIRE', expire)

        return password

    @periodic(600)
    @private
    @job(lock='netdataweb_expire', transient=True, lock_queue_size=1)
    def netdataweb_expire(self, job):
        """
        Generated passwords are placed in the HTTP Basic file and should be valid for 8 hours.
        We allow ourselves a 10 minutes wiggle room for simplicity sake, e.g. token can be valid
        for up to 8 hours and 10 minutes.
        """
        if not os.path.exists(BASIC_FILE):
            return

        try:
            expire = self.middleware.call_sync('cache.get', 'NETDATA_WEB_EXPIRE')
        except KeyError:
            expire = {}

        with HTPASSWD_LOCK:
            ht = HtpasswdFile(BASIC_FILE)
            time_now = int(time.monotonic())
            for user in ht.users():
                if expire_time := expire.get(user):
                    if time_now < expire_time:
                        continue
                # User is not in our cache or expired, should be deleted
                ht.delete(user)

            ht.save()
