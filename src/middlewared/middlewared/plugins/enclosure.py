import asyncio
import errno
import logging
import re
import pathlib
from collections import OrderedDict

from libsg3.ses import EnclosureDevice

from middlewared.schema import Dict, Int, Str, accepts
from middlewared.service import CallError, CRUDService, filterable, private
from middlewared.service_exception import MatchNotFound
import middlewared.sqlalchemy as sa
from middlewared.utils import filter_list
from middlewared.plugins.enclosure_.r30_drive_identify import set_slot_status as r30_set_slot_status
from middlewared.plugins.enclosure_.fseries_drive_identify import set_slot_status as fseries_set_slot_status
from middlewared.plugins.enclosure_.sysfs_disks import toggle_enclosure_slot_identifier


logger = logging.getLogger(__name__)

ENCLOSURE_ACTIONS = {
    'clear': '0x80 0x00 0x00 0x00',
    'identify': '0x80 0x00 0x02 0x00',
    'fault': '0x80 0x00 0x00 0x20',
}

STATUS_DESC = [
    "Unsupported",
    "OK",
    "Critical",
    "Noncritical",
    "Unrecoverable",
    "Not installed",
    "Unknown",
    "Not available",
    "No access allowed",
    "reserved [9]",
    "reserved [10]",
    "reserved [11]",
    "reserved [12]",
    "reserved [13]",
    "reserved [14]",
    "reserved [15]",
]

M_SERIES_REGEX = re.compile(r"(ECStream|iX) 4024S([ps])")
R_SERIES_REGEX = re.compile(r"(ECStream|iX) (FS1|FS2|DSS212S[ps])")
R20_REGEX = re.compile(r"(iX (TrueNAS R20|2012S)p|SMC SC826-P)")
R50_REGEX = re.compile(r"iX eDrawer4048S([12])")
X_SERIES_REGEX = re.compile(r"CELESTIC (P3215-O|P3217-B)")
ES24_REGEX = re.compile(r"(ECStream|iX) 4024J")
ES24F_REGEX = re.compile(r"(ECStream|iX) 2024J([ps])")
MINI_REGEX = re.compile(r"(TRUE|FREE)NAS-MINI")
R20_VARIANT = ["TRUENAS-R20", "TRUENAS-R20A", "TRUENAS-R20B"]


class EnclosureLabelModel(sa.Model):
    __tablename__ = 'truenas_enclosurelabel'

    id = sa.Column(sa.Integer(), primary_key=True)
    encid = sa.Column(sa.String(200), unique=True)
    label = sa.Column(sa.String(200))


class EnclosureService(CRUDService):

    class Config:
        cli_namespace = 'storage.enclosure'

    @filterable
    def query(self, filters, options):
        enclosures = []
        if not self.middleware.call_sync('truenas.is_ix_hardware'):
            # this feature is only available on hardware that ix sells
            return enclosures

        for enc in self.__get_enclosures():
            enclosure = {
                "id": enc.encid,
                "bsg": enc.devname,
                "name": enc.name,
                "model": enc.model,
                "controller": enc.controller,
                "elements": [],
            }

            for name, elems in enc.iter_by_name().items():
                header = None
                elements = []
                has_slot_status = False

                for elem in elems:
                    header = list(elem.get_columns().keys())
                    element = {
                        "slot": elem.slot,
                        "data": dict(zip(elem.get_columns().keys(), elem.get_values())),
                        "name": elem.name,
                        "descriptor": elem.descriptor,
                        "status": elem.status,
                        "value": elem.value,
                        "value_raw": 0x0,
                    }
                    if isinstance(elem.value_raw, int):
                        element["value_raw"] = hex(elem.value_raw)

                    if hasattr(elem, "device_slot_set"):
                        has_slot_status = True
                        element["fault"] = elem.fault
                        element["identify"] = elem.identify

                    elements.append(element)

                if header is not None and elements:
                    enclosure["elements"].append({
                        "name": name,
                        "descriptor": enc.descriptors.get(name, ""),
                        "header": header,
                        "elements": elements,
                        "has_slot_status": has_slot_status
                    })
            # Ensure R50's first expander is first in the list independent of cabling
            if "eDrawer4048S1" in enclosure['name']:
                enclosures.insert(0, enclosure)
            else:
                enclosures.append(enclosure)

        enclosures.extend(self.middleware.call_sync("enclosure.map_nvme"))
        enclosures = self.middleware.call_sync("enclosure.map_enclosures", enclosures)

        for number, enclosure in enumerate(enclosures):
            enclosure["number"] = number

        labels = {
            label["encid"]: label["label"]
            for label in self.middleware.call_sync("datastore.query", "truenas.enclosurelabel")
        }
        for enclosure in enclosures:
            enclosure["label"] = labels.get(enclosure["id"]) or enclosure["name"]

        enclosures = sorted(enclosures, key=lambda enclosure: (0 if enclosure["controller"] else 1, enclosure["id"]))

        return filter_list(enclosures, filters=filters or [], options=options or {})

    @accepts(
        Str("id"),
        Dict(
            "enclosure_update",
            Str("label"),
            update=True,
        ),
    )
    async def do_update(self, id_, data):
        if "label" in data:
            await self.middleware.call("datastore.delete", "truenas.enclosurelabel", [["encid", "=", id_]])
            await self.middleware.call("datastore.insert", "truenas.enclosurelabel", {
                "encid": id_,
                "label": data["label"]
            })

        return await self.get_instance(id_)

    def _get_slot(self, slot_filter, enclosure_query=None, enclosure_info=None):
        if enclosure_info is None:
            enclosure_info = self.middleware.call_sync("enclosure.query", enclosure_query or [])

        for enclosure in enclosure_info:
            try:
                elements = next(filter(lambda element: element["name"] == "Array Device Slot",
                                       enclosure["elements"]))["elements"]
                slot = next(filter(slot_filter, elements))
                return enclosure, slot
            except StopIteration:
                pass

        raise MatchNotFound()

    def _get_slot_for_disk(self, disk, enclosure_info=None):
        return self._get_slot(lambda element: element["data"]["Device"] == disk, enclosure_info=enclosure_info)

    def _get_ses_slot(self, enclosure, element):
        if "original" in element:
            enclosure_id = element["original"]["enclosure_id"]
            slot = element["original"]["slot"]
        else:
            enclosure_id = enclosure["id"]
            slot = element["slot"]

        ses_enclosures = self.__get_enclosures()
        ses_enclosure = ses_enclosures.get_by_encid(enclosure_id)
        if ses_enclosure is None:
            raise MatchNotFound()
        ses_slot = ses_enclosure.get_by_slot(slot)
        if ses_slot is None:
            raise MatchNotFound()
        return ses_slot

    def _get_ses_slot_for_disk(self, disk):
        # This can also return SES slot for disk that is not present in the system
        try:
            enclosure, element = self._get_slot_for_disk(disk)
        except MatchNotFound:
            disk = self.middleware.call_sync(
                "disk.query",
                [["devname", "=", disk]],
                {"get": True, "extra": {"include_expired": True}, "order_by": ["expiretime"]},
            )
            if disk["enclosure"]:
                enclosure, element = self._get_slot(lambda element: element["slot"] == disk["enclosure"]["slot"],
                                                    [["number", "=", disk["enclosure"]["number"]]])
            else:
                raise MatchNotFound()

        return self._get_ses_slot(enclosure, element)

    def _get_orig_enclosure_and_disk(self, enclosure_id, slot, info):
        for i in filter(lambda x: x.get('name') == 'Array Device Slot', info['elements']):
            for j in filter(lambda x: x['slot'] == slot, i['elements']):
                if enclosure_id == 'mapped_enclosure_0':
                    # we've mapped the drive slots in a convenient way for the administrator
                    # to easily be able to identify drive slot 1 (when in reality, it's probably
                    # physically cabled to slot 5 (or whatever))
                    return j['original']['enclosure_bsg'], j['original']['slot']
                else:
                    # a platform that doesn't require mapping the drives so we can just return
                    # the slot passed to us
                    return info['bsg'], slot

    @accepts(Str("enclosure_id"), Int("slot"), Str("status", enum=["CLEAR", "FAULT", "IDENTIFY"]))
    def set_slot_status(self, enclosure_id, slot, status):
        if enclosure_id == 'r30_nvme_enclosure':
            r30_set_slot_status(slot, status)
            return
        elif enclosure_id in ('f60_nvme_enclosure', 'f100_nvme_enclosure', 'f130_nvme_enclosure'):
            fseries_set_slot_status(slot, status)
            return

        try:
            info = self.middleware.call_sync('enclosure.query', [['id', '=', enclosure_id]])[0]
        except IndexError:
            raise CallError(f'Enclosure with id: {enclosure_id!r} not found', errno.ENOENT)

        if info['model'] == 'H Series':
            sysfs_to_ui = {
                1: '8', 2: '9', 3: '10', 4: '11',
                5: '12', 6: '13', 7: '14', 8: '15',
                9: '0', 10: '1', 11: '2', 12: '3',
            }
            if slot not in sysfs_to_ui:
                raise CallError(f'Slot: {slot!r} not found', errno.ENOENT)

            addr = info['bsg'].removeprefix('bsg/')
            sysfs_path = f'/sys/class/enclosure/{addr}'
            mapped_slot = sysfs_to_ui[slot]
            try:
                toggle_enclosure_slot_identifier(sysfs_path, mapped_slot, status, True)
            except FileNotFoundError:
                raise CallError(f'Slot: {slot!r} not found', errno.ENOENT)

            return

        original = self._get_orig_enclosure_and_disk(enclosure_id, slot, info)
        if original is None:
            raise CallError(f'Slot: {slot!r} not found', errno.ENOENT)

        original_bsg, original_slot = original

        if status == 'CLEAR':
            actions = ('clear=ident', 'clear=fault')
        else:
            actions = (f'set={status[:5].lower()}',)

        enc = EnclosureDevice(f'/dev/{original_bsg}')
        try:
            for action in actions:
                enc.set_control(str(original_slot - 1), action)
        except OSError:
            msg = f'Failed to {status} slot {slot!r} on enclosure {info["id"]!r}'
            self.logger.warning(msg, exc_info=True)
            raise CallError(msg)

    @private
    def sync_disk(self, id_, enclosure_info=None, retry=False):
        """
        :param id:
        :param enclosure_info:
        :param retry: retry once more in 60 seconds if no enclosure slot for disk is found
        """
        disk = self.middleware.call_sync(
            'disk.query',
            [['identifier', '=', id_]],
            {'get': True, "extra": {'include_expired': True}}
        )

        try:
            enclosure, element = self._get_slot_for_disk(disk["name"], enclosure_info)
        except MatchNotFound:
            if retry:
                async def delayed():
                    await asyncio.sleep(60)
                    await self.middleware.call('enclosure.sync_disk', id_, enclosure_info)

                self.middleware.run_coroutine(delayed(), wait=False)

                return

            disk_enclosure = None
        else:
            disk_enclosure = {
                "number": enclosure["number"],
                "slot": element["slot"],
            }

        if disk_enclosure != disk['enclosure']:
            self.middleware.call_sync('disk.update', id_, {'enclosure': disk_enclosure})

    def __get_enclosures(self):
        return Enclosures(
            self.middleware.call_sync("enclosure.get_ses_enclosures"),
            self.middleware.call_sync("system.dmidecode_info")["system-product-name"]
        )


class Enclosures(object):

    def __init__(self, stat, product_name):
        self.__enclosures = list()

        if any((
            not isinstance(product_name, str),
            not product_name.startswith(("TRUENAS-", "FREENAS-"))
        )):
            return

        if product_name.startswith("TRUENAS-H"):
            blacklist = list()
        else:
            blacklist = ["VirtualSES"]

        if "-MINI-" not in product_name and product_name not in R20_VARIANT:
            blacklist.append("AHCI SGPIO Enclosure 2.00")

        for num, data in stat.items():
            enclosure = Enclosure(num, data, stat, product_name)
            if any(s in enclosure.encname for s in blacklist):
                continue

            self.__enclosures.append(enclosure)

    def __iter__(self):
        for e in list(self.__enclosures):
            yield e

    def append(self, enc):
        if not isinstance(enc, Enclosure):
            raise ValueError("Not an enclosure")
        self.__enclosures.append(enc)

    def find_device_slot(self, devname):
        for enc in self:
            find = enc.find_device_slot(devname)
            if find is not None:
                return find
        raise AssertionError(f"Enclosure slot not found for {devname}")

    def get_by_id(self, _id):
        for e in self:
            if e.num == _id:
                return e

    def get_by_encid(self, _id):
        for e in self:
            if e.encid == _id:
                return e


class Enclosure(object):

    def __init__(self, num, data, stat, product_name):
        self.num = num
        self.stat = stat
        self.product_name = product_name
        self.devname, data = data
        self.encname = ""
        self.encid = ""
        self.model = ""
        self.controller = False
        self.status = "OK"
        self.__elements = []
        self.__elementsbyname = {}
        self.descriptors = {}
        self._parse(data)

    def _parse(self, data):
        cf, es = data
        self.encname = re.sub(r"\s+", " ", cf.splitlines()[0].strip())
        if m := re.search(r"\s+enclosure logical identifier \(hex\): ([0-9a-f]+)", cf):
            self.encid = m.group(1)

        self._set_model(cf)
        self.status = "OK"
        is_hseries = self.product_name and self.product_name.startswith('TRUENAS-H')
        self.map_disks_to_enclosure_slots(is_hseries)

        element_type = None
        element_number = None
        for line in es.splitlines():
            if m := re.match(r"\s+Element type: (.+), subenclosure", line):
                element_type = m.group(1)

                if element_type != "Audible alarm":
                    element_type = " ".join([
                        word[0].upper() + word[1:]
                        for word in element_type.split()
                    ])
                if element_type == "Temperature Sensor":
                    element_type = "Temperature Sensors"

                element_number = None
            elif m := re.match(r"\s+Element ([0-9]+) descriptor:", line):
                element_number = int(m.group(1))
            elif m := re.match(r"\s+([0-9a-f ]{11})", line):
                if all((element_type, element_number, element_type != 'Array Device Slot')):
                    element = self._enclosure_element(
                        element_number + 1,
                        element_type,
                        self._parse_raw_value(m.group(1)),
                        None,
                        "",
                        "",
                    )
                    if element is not None:
                        self.append(element)

                element_number = None
            else:
                element_number = None

    def map_disks_to_enclosure_slots(self, is_hseries=False):
        """
        The sysfs directory structure is dynamic based on the enclosure that
        is attached.
        Here are some examples of what we've seen on internal hardware:
            /sys/class/enclosure/19:0:6:0/SLOT_001/
            /sys/class/enclosure/13:0:0:0/Drive Slot #0_0000000000000000/
            /sys/class/enclosure/13:0:0:0/Disk #00/
            /sys/class/enclosure/13:0:0:0/Slot 00/
            /sys/class/enclosure/13:0:0:0/slot00/
            /sys/class/enclosure/13:0:0:0/0/

        The safe assumption that we can make on whether or not the directory
        represents a drive slot is looking for the file named "slot" underneath
        each directory. (i.e. /sys/class/enclosure/13:0:0:0/Disk #00/slot)

        If this file doesn't exist, it means 1 thing
            1. this isn't a drive slot directory

        Once we've determined that there is a file named "slot", we can read the
        contents of that file to get the slot number associated to the disk device.
        The "slot" file is always an integer so we don't need to convert to hexadecimal.
        """
        ignore = tuple()
        if is_hseries:
            ignore = ('4', '5', '6', '7')

        mapping = dict()
        pci = self.devname.removeprefix('bsg/')  # why do we set this as 'bsg/13:0:0:0'...?
        for i in filter(lambda x: x.is_dir(), pathlib.Path(f'/sys/class/enclosure/{pci}').iterdir()):
            if is_hseries and i.name in ignore:
                # on hseries platform, the broadcom HBA enumerates sysfs
                # with directory names as the slot number
                # (i.e. /sys/class/enclosure/*/0, /sys/class/enclosure/*/1, etc)
                # There are 16 ports on this card, but we only use 12
                continue

            try:
                slot = int((i / 'slot').read_text().strip())
                slot_status = (i / 'status').read_text().strip()
                ident = (i / 'locate').read_text().strip()
                fault = (i / 'fault').read_text().strip()
            except (FileNotFoundError, ValueError):
                # not a slot directory
                continue
            else:
                try:
                    dev = next((i / 'device/block').iterdir(), '')
                    if dev:
                        dev = dev.name

                    mapping[slot] = (dev, slot_status, ident, fault)
                except FileNotFoundError:
                    # no disk in this slot
                    mapping[slot] = ('', slot_status, ident, fault)

        try:
            if min(mapping) == 0:
                # if the enclosure starts slots at 0 then we need
                # to bump them by 1 to not cause confusion for
                # end-user
                mapping = {k + 1: v for k, v in mapping.items()}
        except ValueError:
            # means mapping is an empty dict (shouldn't happen)
            return

        disk_raw_values = dict()
        if not is_hseries:
            for k, v in EnclosureDevice(f'/dev/{self.devname}').status()['elements'].items():
                if v['type'] == 23 and v['descriptor'] != '<empty>':
                    disk_raw_values[k] = v['status']

        for slot in sorted(mapping):
            disk, slot_status, ident, fault = mapping[slot]
            if is_hseries:
                info = self._enclosure_element(slot, 'Array Device Slot', slot_status, None, '', disk, ident, fault)
            else:
                info = self._enclosure_element(
                    slot,
                    'Array Device Slot',
                    self._parse_raw_value(disk_raw_values.get(slot, [5, 0, 0, 0])),
                    None,
                    '',
                    disk
                )

            if info:
                self.append(info)

        return mapping

    def _set_model(self, data):
        if M_SERIES_REGEX.match(self.encname):
            self.model = "M Series"
            self.controller = True
        elif R_SERIES_REGEX.match(self.encname) or R20_REGEX.match(self.encname) or R50_REGEX.match(self.encname):
            self.model = self.product_name.replace("TRUENAS-", "")
            self.controller = True
        elif self.encname == "AHCI SGPIO Enclosure 2.00":
            if self.product_name in R20_VARIANT:
                self.model = self.product_name.replace("TRUENAS-", "")
                self.controller = True
            elif MINI_REGEX.match(self.product_name):
                # TrueNAS Mini's do not have their product name stripped
                self.model = self.product_name
                self.controller = True
            self.controller = True
        elif X_SERIES_REGEX.match(self.encname):
            self.model = "X Series"
            self.controller = True
        elif self.encname.startswith("BROADCOM VirtualSES 03"):
            self.model = "H Series"
            self.controller = True
        elif self.encname.startswith("QUANTA JB9 SIM"):
            self.model = "E60"
        elif self.encname.startswith("Storage 1729"):
            self.model = "E24"
        elif self.encname.startswith("ECStream 3U16+4R-4X6G.3"):
            if "SD_9GV12P1J_12R6K4" in data:
                self.model = "Z Series"
                self.controller = True
            else:
                self.model = "E16"
        elif self.encname.startswith("ECStream 3U16RJ-AC.r3"):
            self.model = "E16"
        elif self.encname.startswith("HGST H4102-J"):
            self.model = "ES102"
        elif self.encname.startswith((
            "VikingES NDS-41022-BB",
            "VikingES VDS-41022-BB",
        )):
            self.model = "ES102G2"
        elif self.encname.startswith("CELESTIC R0904"):
            self.model = "ES60"
        elif self.encname.startswith("HGST H4060-J"):
            self.model = "ES60G2"
        elif ES24_REGEX.match(self.encname):
            self.model = "ES24"
        elif ES24F_REGEX.match(self.encname):
            self.model = "ES24F"
        elif self.encname.startswith("CELESTIC X2012"):
            self.model = "ES12"

    def _parse_raw_value(self, value):
        if isinstance(value, str):
            value = [int(i.replace("0x", ""), 16) for i in value.split(' ')]

        newvalue = 0
        for i, v in enumerate(value):
            newvalue |= v << (2 * (3 - i)) * 4
        return newvalue

    def iter_by_name(self):
        return OrderedDict(sorted(self.__elementsbyname.items()))

    def append(self, element):
        self.__elements.append(element)
        if element.name not in self.__elementsbyname:
            self.__elementsbyname[element.name] = [element]
        else:
            self.__elementsbyname[element.name].append(element)
        element.enclosure = self

    def _enclosure_element(self, slot, name, value, status, desc, dev, ident=None, fault=None):

        if name == "Audible alarm":
            return AlarmElm(slot=slot, value_raw=value, desc=desc)
        elif name == "Communication Port":
            return CommPort(slot=slot, value_raw=value, desc=desc)
        elif name == "Current Sensor":
            return CurrSensor(slot=slot, value_raw=value, desc=desc)
        elif name == "Enclosure":
            return EnclosureElm(slot=slot, value_raw=value, desc=desc)
        elif name == "Voltage Sensor":
            return VoltSensor(slot=slot, value_raw=value, desc=desc)
        elif name == "Cooling":
            return Cooling(slot=slot, value_raw=value, desc=desc)
        elif name == "Temperature Sensors":
            return TempSensor(slot=slot, value_raw=value, desc=desc)
        elif name == "Power Supply":
            return PowerSupply(slot=slot, value_raw=value, desc=desc)
        elif name == "Array Device Slot":
            # Echostream have actually only 16 physical disk slots
            # See #24254
            if self.encname.startswith('ECStream 3U16+4R-4X6G.3') and slot > 16:
                return
            if self.model.startswith('R50, ') and slot >= 25:
                return
            return ArrayDevSlot(slot=slot, value_raw=value, desc=desc, dev=dev, identify=ident, fault=fault)

        elif name == "SAS Connector":
            return SASConnector(slot=slot, value_raw=value, desc=desc)
        elif name == "SAS Expander":
            return SASExpander(slot=slot, value_raw=value, desc=desc)
        else:
            return Element(slot=slot, name=name, value_raw=value, desc=desc)

    def __unicode__(self):
        return self.name

    def __repr__(self):
        return f'<Enclosure: {self.name}>'

    def __iter__(self):
        for e in list(self.__elements):
            yield e

    @property
    def name(self):
        return self.encname

    def find_device_slot(self, devname):
        """
        Get the element that the device name points to
        getencstat /dev/ses0 | grep da6
        Element 0x7: Array Device Slot, status: OK (0x01 0x00 0x00 0x00),
        descriptor: 'Slot 07', dev: 'da6,pass6'
        What we are interested in is the 0x7

        Returns:
            A tuple of the form (Enclosure-slot-number, element)

        Raises:
            AssertionError: enclosure slot not found
        """
        for e in self.__elementsbyname.get('Array Device Slot', []):
            if e.devname == devname:
                return e

    def get_by_slot(self, slot):
        for e in self:
            if e.slot == slot:
                return e


class Element(object):

    def __init__(self, **kwargs):
        if 'name' in kwargs:
            self.name = kwargs.pop('name')
        self.value_raw = kwargs.pop('value_raw')
        self.slot = kwargs.pop('slot')
        if isinstance(self.value_raw, int):
            self.status_raw = (self.value_raw >> 24) & 0xf
        else:
            self.status_raw = self.value_raw
            self._identify = kwargs.pop('identify')
            self._fault = kwargs.pop('fault')

        try:
            self.descriptor = kwargs.pop('desc')
        except Exception:
            self.descriptor = 'Unknown'
        self.enclosure = None

    def __repr__(self):
        return f'<Element: {self.name}>'

    def get_columns(self):
        return OrderedDict([
            ('Descriptor', lambda y: y.descriptor),
            ('Status', lambda y: y.status),
            ('Value', lambda y: y.value),
        ])

    def get_values(self):
        for value in list(self.get_columns().values()):
            yield value(self)

    @property
    def value(self):
        return self.value_raw & 0xffff

    @property
    def status(self):
        if isinstance(self.status_raw, str):
            return self.status_raw
        else:
            return STATUS_DESC[self.status_raw]


class AlarmElm(Element):
    name = "Audible alarm"

    @property
    def identify(self):
        return (self.value_raw >> 16) & 0x80

    @property
    def fail(self):
        return (self.value_raw >> 16) & 0x40

    @property
    def rqmute(self):
        return self.value_raw & 0x80

    @property
    def muted(self):
        return self.value_raw & 0x40

    @property
    def remind(self):
        return self.value_raw & 0x10

    @property
    def info(self):
        return self.value_raw & 0x08

    @property
    def noncrit(self):
        return self.value_raw & 0x04

    @property
    def crit(self):
        return self.value_raw & 0x02

    @property
    def unrec(self):
        return self.value_raw & 0x01

    @property
    def value(self):
        output = []
        if self.identify:
            output.append("Identify on")

        if self.fail:
            output.append("Fail on")

        if self.rqmute:
            output.append("RQST mute")

        if self.muted:
            output.append("Muted")

        if self.remind:
            output.append("Remind")

        if self.info:
            output.append("INFO")

        if self.noncrit:
            output.append("NON-CRIT")

        if self.crit:
            output.append("CRIT")

        if self.unrec:
            output.append("UNRECOV")

        if not output:
            output.append("None")
        return ', '.join(output)


class CommPort(Element):
    name = "Communication Port"

    @property
    def identify(self):
        return (self.value_raw >> 16) & 0x80

    @property
    def fail(self):
        return (self.value_raw >> 16) & 0x40

    @property
    def disabled(self):
        return self.value_raw & 0x01

    @property
    def value(self):
        output = []
        if self.identify:
            output.append("Identify on")

        if self.fail:
            output.append("Fail on")

        if self.disabled:
            output.append("Disabled")

        if not output:
            output.append("None")
        return ', '.join(output)


class CurrSensor(Element):
    name = "Current Sensor"

    @property
    def identify(self):
        return (self.value_raw >> 16) & 0x80

    @property
    def fail(self):
        return (self.value_raw >> 16) & 0x40

    @property
    def warnover(self):
        return (self.value_raw >> 16) & 0x8

    @property
    def critover(self):
        return (self.value_raw >> 16) & 0x2

    @property
    def value(self):
        output = []
        output.append("%sA" % ((self.value_raw & 0xffff) / 100))

        if self.identify:
            output.append("Identify on")

        if self.fail:
            output.append("Fail on")

        if self.warnover:
            output.append("Warn over")

        if self.critover:
            output.append("Crit over")

        return ', '.join(output)


class EnclosureElm(Element):
    name = "Enclosure"

    @property
    def identify(self):
        return (self.value_raw >> 16) & 0x80

    @property
    def pctime(self):
        return (self.value_raw >> 10) & 0x3f

    @property
    def potime(self):
        return (self.value_raw >> 2) & 0x3f

    @property
    def failind(self):
        return (self.value_raw >> 8) & 0x02

    @property
    def warnind(self):
        return (self.value_raw >> 8) & 0x01

    @property
    def value(self):
        output = []
        if self.identify:
            output.append("Identify on")

        if self.failind:
            output.append("Fail on")

        if self.warnind:
            output.append("Warn on")

        if self.pctime:
            output.append(f"Power cycle {self.pctime} min, power off for {self.potime} min")

        if not output:
            output.append("None")
        return ', '.join(output)


class VoltSensor(Element):
    name = "Voltage Sensor"

    @property
    def identify(self):
        return (self.value_raw >> 16) & 0x80

    @property
    def fail(self):
        return (self.value_raw >> 16) & 0x40

    @property
    def warnover(self):
        return (self.value_raw >> 16) & 0x8

    @property
    def warnunder(self):
        return (self.value_raw >> 16) & 0x4

    @property
    def critover(self):
        return (self.value_raw >> 16) & 0x2

    @property
    def critunder(self):
        return (self.value_raw >> 16) & 0x1

    @property
    def value(self):
        output = []
        output.append("%sV" % ((self.value_raw & 0xffff) / 100))

        if self.identify:
            output.append("Identify on")

        if self.fail:
            output.append("Fail on")

        if self.warnover:
            output.append("Warn over")

        if self.warnunder:
            output.append("Warn under")

        if self.critover:
            output.append("Crit over")

        if self.critunder:
            output.append("Crit under")

        return ', '.join(output)


class Cooling(Element):
    name = "Cooling"

    @property
    def value(self):
        return "%s RPM" % (((self.value_raw & 0x7ff00) >> 8) * 10)


class TempSensor(Element):
    name = "Temperature Sensor"

    @property
    def value(self):
        value = (self.value_raw & 0xff00) >> 8
        if not value:
            value = None
        else:
            # 8 bits represents -19 C to +235 C */
            # value of 0 (would imply -20 C) reserved */
            value -= 20
            value = "%dC" % value
        return value


class PowerSupply(Element):
    name = "Power Supply"

    @property
    def identify(self):
        return (self.value_raw >> 16) & 0x80

    @property
    def overvoltage(self):
        return (self.value_raw >> 8) & 0x8

    @property
    def undervoltage(self):
        return (self.value_raw >> 8) & 0x4

    @property
    def overcurrent(self):
        return (self.value_raw >> 8) & 0x2

    @property
    def fail(self):
        return self.value_raw & 0x40

    @property
    def off(self):
        return self.value_raw & 0x10

    @property
    def tempfail(self):
        return self.value_raw & 0x8

    @property
    def tempwarn(self):
        return self.value_raw & 0x4

    @property
    def acfail(self):
        return self.value_raw & 0x2

    @property
    def dcfail(self):
        return self.value_raw & 0x1

    @property
    def value(self):
        output = []
        if self.identify:
            output.append("Identify on")

        if self.fail:
            output.append("Fail on")

        if self.overvoltage:
            output.append("DC overvoltage")

        if self.undervoltage:
            output.append("DC undervoltage")

        if self.overcurrent:
            output.append("DC overcurrent")

        if self.tempfail:
            output.append("Overtemp fail")

        if self.tempwarn:
            output.append("Overtemp warn")

        if self.acfail:
            output.append("AC fail")

        if self.dcfail:
            output.append("DC fail")

        if not output:
            output.append("None")
        return ', '.join(output)


class ArrayDevSlot(Element):
    name = "Array Device Slot"

    def __init__(self, dev=None, **kwargs):
        super(ArrayDevSlot, self).__init__(**kwargs)
        dev = [y for y in dev.strip().split(',') if not y.startswith('pass')]
        if dev:
            self.devname = dev[0]
        else:
            self.devname = ''

    def get_columns(self):
        columns = super(ArrayDevSlot, self).get_columns()
        columns['Device'] = lambda y: y.devname
        return columns

    def device_slot_set(self, status):
        """
        Actually issue the command to set ``status'' in a given `slot''
        of the enclosure number ``encnumb''

        Returns:
            True if the command succeeded, False otherwise
        """
        # Impossible to be used in an efficient way so it's a NO-OP
        return True

    @property
    def identify(self):
        if hasattr(self, '_identify'):
            return self._identify != '0'
        elif (self.value_raw >> 8) & 0x2:
            return True
        else:
            return False

    @property
    def fault(self):
        if hasattr(self, '_fault'):
            return self._fault != '0'
        elif self.value_raw & 0x20:
            return True
        else:
            return False

    @property
    def value(self):
        output = []
        if self.identify:
            output.append("Identify on")

        if self.fault:
            output.append("Fault on")

        if not output:
            output.append("None")
        return ', '.join(output)


class SASConnector(Element):
    name = "SAS Connector"

    @property
    def type(self):
        """
        Determine the type of the connector

        Based on sysutils/sg3-utils source code
        """
        conn_type = (self.value_raw >> 16) & 0x7f
        if conn_type == 0x0:
            return "No information"
        elif conn_type == 0x1:
            return "SAS 4x receptacle (SFF-8470) [max 4 phys]"
        elif conn_type == 0x2:
            return "Mini SAS 4x receptacle (SFF-8088) [max 4 phys]"
        elif conn_type == 0x3:
            return "QSFP+ receptacle (SFF-8436) [max 4 phys]"
        elif conn_type == 0x4:
            return "Mini SAS 4x active receptacle (SFF-8088) [max 4 phys]"
        elif conn_type == 0x5:
            return "Mini SAS HD 4x receptacle (SFF-8644) [max 4 phys]"
        elif conn_type == 0x6:
            return "Mini SAS HD 8x receptacle (SFF-8644) [max 8 phys]"
        elif conn_type == 0x7:
            return "Mini SAS HD 16x receptacle (SFF-8644) [max 16 phys]"
        elif conn_type == 0xf:
            return "Vendor specific external connector"
        elif conn_type == 0x10:
            return "SAS 4i plug (SFF-8484) [max 4 phys]"
        elif conn_type == 0x11:
            return "Mini SAS 4i receptacle (SFF-8087) [max 4 phys]"
        elif conn_type == 0x12:
            return "Mini SAS HD 4i receptacle (SFF-8643) [max 4 phys]"
        elif conn_type == 0x13:
            return "Mini SAS HD 8i receptacle (SFF-8643) [max 8 phys]"
        elif conn_type == 0x20:
            return "SAS Drive backplane receptacle (SFF-8482) [max 2 phys]"
        elif conn_type == 0x21:
            return "SATA host plug [max 1 phy]"
        elif conn_type == 0x22:
            return "SAS Drive plug (SFF-8482) [max 2 phys]"
        elif conn_type == 0x23:
            return "SATA device plug [max 1 phy]"
        elif conn_type == 0x24:
            return "Micro SAS receptacle [max 2 phys]"
        elif conn_type == 0x25:
            return "Micro SATA device plug [max 1 phy]"
        elif conn_type == 0x26:
            return "Micro SAS plug (SFF-8486) [max 2 phys]"
        elif conn_type == 0x27:
            return "Micro SAS/SATA plug (SFF-8486) [max 2 phys]"
        elif conn_type == 0x2f:
            return "SAS virtual connector [max 1 phy]"
        elif conn_type == 0x3f:
            return "Vendor specific internal connector"
        else:
            if conn_type < 0x10:
                return "unknown external connector type: 0x%x" % conn_type
            elif conn_type < 0x20:
                return "unknown internal wide connector type: 0x%x" % conn_type
            elif conn_type < 0x30:
                return (
                    "unknown internal connector to end device, type: 0x%x" % (
                        conn_type,
                    )
                )
            elif conn_type < 0x3f:
                return "reserved for internal connector, type:0x%x" % conn_type
            elif conn_type < 0x70:
                return "reserved connector type: 0x%x" % conn_type
            elif conn_type < 0x80:
                return "vendor specific connector type: 0x%x" % conn_type
            else:
                return "unexpected connector type: 0x%x" % conn_type

    @property
    def fail(self):
        if self.value_raw & 0x40:
            return True
        return False

    @property
    def value(self):
        output = [self.type]
        if self.fail:
            output.append("Fail on")
        return ', '.join(output)


class SASExpander(Element):
    name = "SAS Expander"

    @property
    def identify(self):
        return (self.value_raw >> 16) & 0x80

    @property
    def fail(self):
        return (self.value_raw >> 16) & 0x40

    @property
    def value(self):
        output = []
        if self.identify:
            output.append("Identify on")

        if self.fail:
            output.append("Fail on")

        if not output:
            output.append("None")
        return ', '.join(output)
