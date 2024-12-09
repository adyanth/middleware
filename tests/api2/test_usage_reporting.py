import pytest
from itertools import chain
from middlewared.test.integration.assets.nfs import nfs_server
from middlewared.test.integration.assets.ftp import ftp_server
from middlewared.test.integration.assets.pool import dataset as nfs_dataset
from middlewared.test.integration.utils import call
from middlewared.test.integration.utils.client import truenas_server
from protocols import ftp_connection, SSH_NFS, nfs_share

from auto_config import password, pool_name, user


class GatherTypes:
    expected = {
        'total_capacity': ['total_capacity'],
        'backup_data': ['data_backup_stats', 'data_without_backup_size'],
        'applications': ['apps', 'catalog_items', 'docker_images'],
        'filesystem_usage': ['datasets', 'zvols'],
        'ha_stats': ['ha_licensed'],
        'directory_service_stats': ['directory_services'],
        'cloud_services': ['cloud_services'],
        'hardware': ['hardware'],
        'network': ['network'],
        'system_version': ['platform', 'version'],
        'system': ['system_hash', 'usage_version', 'system'],
        'pools': ['pools', 'total_raw_capacity'],
        'services': ['services'],
        'nfs': ['NFS'],
        'ftp': ['FTP'],
        'sharing': ['shares'],
        'vms': ['vms'],
        'nspawn_containers': ['nspawn_containers'],
        'vendor_info': ['is_vendored', 'vendor_name']
        # Add new gather type here
    }


@pytest.fixture(scope="module")
def get_usage_sample():
    sample = call('usage.gather')
    yield sample


def test_gather_types(get_usage_sample):
    """ Confirm we find the expected types. Fail if this test needs updating """
    sample = get_usage_sample
    expected = list(chain.from_iterable(GatherTypes.expected.values()))

    # If there is a mismatch it probably means this test module needs to be updated
    assert set(expected).symmetric_difference(sample) == set(), "Expected empty set. "\
        f"Missing an entry in the output ({len(sample)} entries) or test needs updating ({len(expected)} entries)"


def test_nfs_reporting(get_usage_sample):
    """ Confirm we are correctly reporting the number of connections """
    # Initial state should have NFSv[3,4] and no connections
    assert set(get_usage_sample['NFS']['enabled_protocols']) == set(["NFSV3", "NFSV4"])
    assert get_usage_sample['NFS']['num_clients'] == 0

    # Establish two connections
    nfs_path = f'/mnt/{pool_name}/test_nfs'
    with nfs_dataset("test_nfs"):
        with nfs_share(nfs_path):
            with nfs_server():
                with SSH_NFS(truenas_server.ip, nfs_path,
                             user=user, password=password, ip=truenas_server.ip):
                    usage_sample = call('usage.gather')
                    assert usage_sample['NFS']['num_clients'] == 1


def test_ftp_reporting(get_usage_sample):
    """ Confirm we are correctly reporting the number of connections """
    # Initial state should have no connections
    assert get_usage_sample['FTP']['num_connections'] == 0

    # Establish two connections
    with ftp_server():
        with ftp_connection(truenas_server.ip):
            with ftp_connection(truenas_server.ip):
                usage_sample = call('usage.gather')
                assert usage_sample['FTP']['num_connections'] == 2


# Possible TODO:  Add validation of the entries
