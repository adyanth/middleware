import re

from middlewared.rclone.base import BaseRcloneRemote
from middlewared.schema import Str
from middlewared.validators import Match, URL, JsonSchema, Enum

class OneDriveRcloneRemote(BaseRcloneRemote):
    '''
    OneDrive Backend for Rclone. See https://rclone.org/onedrive/ for configuration instructions.
    Use rclone locally to get the credentials easily.
    '''
    name = "ONEDRIVE"
    title = "Microsoft OneDrive Cloud Storage"

    buckets = True

    fast_list = True

    rclone_type = "onedrive"

    credentials_schema = [
        Str("token", title="JSON Token", required=True, validators=[
            JsonSchema([
                "access_token", "refresh_token", "expiry",
            ]),
        ]),
        Str("drive_id", title="Drive ID", required=True),
        Str("drive_type", title="Drive Type", default="business", validators=[
            Enum("personal", "business", "documentLibrary")
        ]),
    ]

    async def get_task_extra(self, task):
        return {"chunk_size": "100M"}
