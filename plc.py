import time
import requests

from pymodbus.client.sync import ModbusTcpClient

import config


# ============================================================
# FLOW CONFIGURATION CACHE
# ============================================================

_flow_cache = None
_flow_cache_time = 0


# ============================================================
# TAG SCHEDULER / TRIGGER STATE
# ============================================================

_next_read_time = {}
_scheduler_signature = None
_trigger_memory = {}


# ============================================================
# PLC CLIENT
# ============================================================

_client = None
_client_config = None


# ============================================================
# GET FLOW CONFIGURATION FROM VPS
# ============================================================

def get_flow_config(force=False):
    global _flow_cache, _flow_cache_time

    now = time.time()
    if (
        not force
        and _flow_cache is not None
        and (now - _flow_cache_time) < config.FLOW_REFRESH_INTERVAL
    ):
        return _flow_cache

    url = config.SERVER_URL.rstrip("/") + "/api/edge/config"
    try:
        response = requests.get(url, params={"PLC_ID": config.PLC_ID}, timeout=5)
        response.raise_for_status()
        flow = response.json()
        if not flow:
            print("EMPTY FLOW CONFIGURATION")
            return _flow_cache
        _flow_cache = flow
        _flow_cache_time = now
        print("FLOW CONFIG RECEIVED")
        return flow
    except Exception as e:
        print("FLOW CONFIG ERROR:", e)
        return _flow_cache


# ============================================================
# EXTRACT RUNTIME CONFIGURATION
# ============================================================

def get_runtime_configuration():
    flow = get_flow_config()
    if not flow:
        return None, []

    try:
        nodes = flow["drawflow"]["Home"]["data"]
    except Exception as e:
        print("FLOW FORMAT ERROR:", e)
        return None, []

    plc_config = None
    for node in nodes.values():
        node_type = node.get("class") or node.get("name")
        if node_type != "PLCReader":
            continue
        data = node.get("data", {}) or {}
        required = ["ip", "port", "slave", "register", "count"]
        missing = [k for k in required if data.get(k) in (None, "")]
        if missing:
            print("PLCReader CONFIGURATION INCOMPLETE:", missing)
            return None, []
        try:
            port = int(data["port"])
            slave = int(data["slave"])
            register = int(data["register"])
            count = int(data["count"])
        except Exception as e:
            print("PLCReader CONFIGURATION ERROR:", e)
            return None, []
        if port <= 0 or slave < 0 or register < 0 or count <= 0:
            print("INVALID PLC CONFIGURATION")
            return None, []
        plc_config = {
            "ip": str(data["ip"]),
            "port": port,
            "slave": slave,
            "register": register,
            "count": count,
        }
        break

    if plc_config is None:
        print("NO PLCReader NODE FOUND")
        return None, []

    mappings = []
    for node in nodes.values():
        node_type = node.get("class") or node.get("name")
        if node_type != "TagMapper":
            continue
        data = node.get("data", {}) or {}
        for mapping in data.get("mappings", []) or []:
            if not isinstance(mapping, dict):
                continue
            if mapping.get("register") is None:
                continue
            name = str(mapping.get("name", "")).strip()
            if not name:
                continue
            try:
                register = int(mapping["register"])
            except Exception:
                print("INVALID TAG REGISTER:", mapping)
                continue
            try:
                scale = float(mapping.get("scale", 1))
            except Exception:
                scale = 1.0
            try:
                interval = float(mapping.get("interval", 1))
            except Exception:
                interval = 1.0
            if interval <= 0:
                interval = 1.0
            storage = str(mapping.get("storage", "TIME")).upper()
            try:
                trigger_register = int(mapping.get("trigger_register", 0))
            except Exception:
                trigger_register = 0
            mappings.append({
                "register": register,
                "name": name,
                "datatype": str(mapping.get("datatype", "INT")).upper(),
                "scale": scale,
                "storage": storage,
                "interval": interval,
                "trigger_register": trigger_register,
                "trigger_value": mapping.get("trigger_value", 0),
            })
        break

    if not mappings:
        print("NO TAG MAPPINGS FOUND")
        return plc_config, []

    return plc_config, mappings


# ============================================================
# PLC CLIENT MANAGEMENT
# ============================================================

def get_client(plc_config):
    global _client, _client_config

    current_config = (plc_config["ip"], plc_config["port"], plc_config["slave"])
    if _client_config != current_config:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None
        _client_config = current_config
        print("PLC CLIENT CONFIGURATION UPDATED:", current_config)

    if _client is None:
        _client = ModbusTcpClient(plc_config["ip"], port=plc_config["port"], timeout=3)

    try:
        if not _client.is_socket_open() and not _client.connect():
            print("PLC CONNECTION FAILED:", plc_config["ip"], plc_config["port"])
            return None
    except Exception as e:
        print("PLC CONNECTION ERROR:", e)
        return None

    return _client


# ============================================================
# READ ONE PLC REGISTER
# ============================================================

def read_register(client, address, slave):
    try:
        try:
            result = client.read_holding_registers(address=int(address), count=1, unit=int(slave))
        except TypeError:
            result = client.read_holding_registers(address=int(address), count=1, slave=int(slave))

        if result.isError() or not getattr(result, "registers", None):
            return None
        return result.registers[0]
    except Exception as e:
        print("PLC READ ERROR:", e)
        return None


# ============================================================
# CREATE SCHEDULER SIGNATURE
# ============================================================

def update_scheduler(mappings):
    global _scheduler_signature, _next_read_time, _trigger_memory

    signature = tuple(
        (
            m["name"], m["register"], m["interval"], m["datatype"],
            m["scale"], m["storage"], m["trigger_register"], str(m["trigger_value"])
        )
        for m in mappings
    )
    if signature == _scheduler_signature:
        return

    old_schedule = dict(_next_read_time)
    old_trigger_memory = dict(_trigger_memory)
    now = time.time()
    new_schedule = {}
    new_trigger_memory = {}

    for mapping in mappings:
        name = mapping["name"]
        storage = str(mapping.get("storage", "TIME")).upper()
        if storage == "TRIGGER":
            # Never schedule trigger tags by interval.
            new_schedule[name] = old_schedule.get(name, float("inf"))
            if name in old_trigger_memory:
                new_trigger_memory[name] = old_trigger_memory[name]
        else:
            new_schedule[name] = old_schedule.get(name, now)

    _scheduler_signature = signature
    _next_read_time = new_schedule
    _trigger_memory = new_trigger_memory
    print("TAG SCHEDULE UPDATED")


# ============================================================
# CHECK IF TIME TAG IS DUE
# ============================================================

def tag_is_due(mapping, now):
    if str(mapping.get("storage", "TIME")).upper() == "TRIGGER":
        return False
    return now >= _next_read_time.get(mapping["name"], 0)


# ============================================================
# UPDATE NEXT EXECUTION TIME
# ============================================================

def schedule_next(mapping, now):
    try:
        interval = float(mapping.get("interval", 1))
    except Exception:
        interval = 1.0
    if interval <= 0:
        interval = 1.0
    _next_read_time[mapping["name"]] = now + interval


# ============================================================
# TRIGGER EDGE DETECTION
# ============================================================

def trigger_reached(mapping, current):
    name = mapping["name"]
    previous = _trigger_memory.get(name)
    _trigger_memory[name] = current

    try:
        previous_number = None if previous is None else float(previous)
        current_number = float(current)
        target = float(mapping.get("trigger_value", 0))
        return previous_number == 0 and current_number == target
    except (TypeError, ValueError):
        return previous == 0 and current == mapping.get("trigger_value", 0)


# ============================================================
# CONVERT DATATYPE
# ============================================================

def convert_value(value, mapping):
    try:
        value = float(value) * float(mapping.get("scale", 1))
    except Exception:
        pass

    datatype = str(mapping.get("datatype", "INT")).upper()
    if datatype == "INT":
        return int(value)
    if datatype == "FLOAT":
        return float(value)
    if datatype == "BOOL":
        return bool(value)
    return value


# ============================================================
# READ ALL DUE TAGS
# ============================================================

def read_all():
    plc_config, mappings = get_runtime_configuration()
    if not plc_config or not mappings:
        return {}

    update_scheduler(mappings)
    now = time.time()

    time_mappings = [
        m for m in mappings
        if str(m.get("storage", "TIME")).upper() != "TRIGGER" and tag_is_due(m, now)
    ]
    trigger_mappings = [
        m for m in mappings
        if str(m.get("storage", "TIME")).upper() == "TRIGGER"
    ]

    if not time_mappings and not trigger_mappings:
        return {}

    client = get_client(plc_config)
    if client is None:
        return {}

    slave = plc_config["slave"]
    data = {}

    # TIME tags: normal interval-based acquisition.
    for mapping in time_mappings:
        register = mapping["register"]
        name = mapping["name"]
        value = read_register(client, register, slave)
        schedule_next(mapping, now)

        if value is None:
            continue
        try:
            value = convert_value(value, mapping)
        except Exception as e:
            print("VALUE CONVERSION ERROR:", name, e)
            continue

        data[name] = value
        print("DUE:", name, value, "REGISTER:", register, "INTERVAL:", mapping.get("interval", 1))

    # TRIGGER tags: monitor only their trigger register.
    # The actual tag register is read ONLY after a 0 -> trigger_value edge.
    trigger_register_cache = {}
    for mapping in trigger_mappings:
        name = mapping["name"]
        trigger_register = mapping.get("trigger_register")
        if trigger_register is None:
            continue
        try:
            trigger_register = int(trigger_register)
        except (TypeError, ValueError):
            print("INVALID TRIGGER REGISTER:", name, trigger_register)
            continue

        if trigger_register not in trigger_register_cache:
            trigger_register_cache[trigger_register] = read_register(client, trigger_register, slave)

        trigger_current = trigger_register_cache[trigger_register]
        if trigger_current is None:
            continue

        if not trigger_reached(mapping, trigger_current):
            continue

        register = mapping["register"]
        value = read_register(client, register, slave)
        if value is None:
            continue

        try:
            value = convert_value(value, mapping)
        except Exception as e:
            print("TRIGGER VALUE CONVERSION ERROR:", name, e)
            continue

        data[name] = value
        print(
            "TRIGGER:", name, value,
            "REGISTER:", register,
            "TRIGGER REGISTER:", trigger_register,
            "TRIGGER VALUE:", mapping.get("trigger_value", 0)
        )

    return data
