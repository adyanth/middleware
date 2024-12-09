import contextlib

from functions import DELETE, POST

from .ftp_proto import ftp_connect, ftp_connection, ftps_connect, ftps_connection  # noqa
from .iscsi_proto import ISCSIDiscover, initiator_name_supported, iscsi_scsi_connect, iscsi_scsi_connection  # noqa
from .iSNSP.client import iSNSPClient
from .ms_rpc import MS_RPC  # noqa
from .nfs_proto import SSH_NFS  # noqa
from .smb_proto import SMB


@contextlib.contextmanager
def smb_connection(**kwargs):
    c = SMB()
    c.connect(**kwargs)

    try:
        yield c
    finally:
        c.disconnect()


@contextlib.contextmanager
def smb_share(path, options=None):
    results = POST("/sharing/smb/", {
        "path": path,
        **(options or {}),
    })
    assert results.status_code == 200, results.text
    id = results.json()["id"]

    try:
        yield id
    finally:
        result = DELETE(f"/sharing/smb/id/{id}/")
        assert result.status_code == 200, result.text


@contextlib.contextmanager
def nfs_share(path, options=None):
    results = POST("/sharing/nfs/", {
        "path": path,
        **(options or {}),
    })
    assert results.status_code == 200, results.text
    id = results.json()["id"]

    try:
        yield id
    finally:
        result = DELETE(f"/sharing/nfs/id/{id}/")
        assert result.status_code == 200, result.text


@contextlib.contextmanager
def isns_connection(host, initiator_iqn, **kwargs):
    c = iSNSPClient(host, initiator_iqn, **kwargs)
    try:
        c.connect()
        c.register_initiator()
        yield c
    finally:
        c.deregister_initiator()
        c.close()
