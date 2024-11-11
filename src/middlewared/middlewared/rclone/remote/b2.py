import textwrap

from middlewared.rclone.base import BaseRcloneRemote
from middlewared.schema import Int
from middlewared.validators import Range


class B2RcloneRemote(BaseRcloneRemote):
    name = "B2"
    title = "Backblaze B2"

    buckets = True

    fast_list = True

    rclone_type = "b2"

    task_schema = [
        Int("chunk_size", title="Upload chunk size (in megabytes)", description=textwrap.dedent("""\
            Upload chunk size. Must fit in memory. Note that these chunks are buffered in memory and there might be a
            maximum of «--transfers» chunks in progress at once. Also, your largest file must be split in no more
            than 10 000 chunks.
        """), default=96, validators=[Range(min_=5)]),
    ]

    async def get_task_extra(self, task):
        chunk_size = task["attributes"].get("chunk_size", 96)
        extra = {"chunk_size": f"{chunk_size}M"}
        if chunk_size > 200:
            extra["upload_cutoff"] = f"{chunk_size}M"
        return extra

    async def get_task_extra_args(self, task):
        chunk_size = task["attributes"].get("chunk_size", 96)
        if chunk_size > 128:
            return [f"--multi-thread-cutoff={chunk_size * 2 + 1}M"]

        return []
