
from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO
import socket
import sys
import json, io, csv
import time
import threading
import logging
from logging.handlers import RotatingFileHandler
from smartcard.System import readers
from smartcard.util import toHexString, toBytes
from smartcard.CardConnectionObserver import ConsoleCardConnectionObserver
import os

# TCP keep alive
def set_tcp_keepalive(sock: socket.socket, *, idle=60, interval=15, count=4):
    """
    Enable TCP keepalives so NATs/routers don't drop idle connections.
    Safe to call on any socket; Linux gets extra tunables.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform.startswith("linux"):
            TCP_KEEPIDLE = getattr(socket, "TCP_KEEPIDLE", 4)
            TCP_KEEPINTVL = getattr(socket, "TCP_KEEPINTVL", 5)
            TCP_KEEPCNT = getattr(socket, "TCP_KEEPCNT", 6)
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPIDLE, idle)
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPINTVL, interval)
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPCNT, count)
    except Exception as e:
        logging.warning(f"Keepalive setup failed: {e}")

app = Flask(__name__)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    ping_interval=20,
    ping_timeout=60,
    async_mode="threading"
)

# Custom SocketIO Handler for emitting logs
class SocketIOHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            socketio.emit('log_message', {'data': msg}, namespace='/logs')
        except Exception as e:
            print(f"SocketIOHandler error: {e}")

# Configure root logger
logging.getLogger('').setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(formatter)

# SocketIO handler
socketio_handler = SocketIOHandler()
socketio_handler.setLevel(logging.DEBUG)
socketio_handler.setFormatter(formatter)

# Add handlers
logging.getLogger('').addHandler(console_handler)
logging.getLogger('').addHandler(socketio_handler)

# Configuration
SERVER_IP = "206.189.24.200"
SERVER_PORT = 20119
APP_ID = "r06"          # unique app id
DEVICE_ID = "rack16"    # human-readable name
WEB_PORT = 5000         # port Flask will listen on
DEVICE_OFFSET = 0

APDU_COMMANDS = [
    "00A4020C020002",
    "00B0000118",
    "00A4020C020005",
    "00B0000008"
]

def reader_no(idx: int) -> int:
    return DEVICE_OFFSET + idx + 1

REQUEST_INTERVAL = 1
SOCKET_RETRY_INTERVAL = 5
SOCKET_RETRY_TIMEOUT = 60
REMOVAL_GRACE_SEC = 3.0
MAX_CONNECT_BACKOFF = 10.0
READER_INDEX_MAPPING = {}
reader_data = {}
is_running = False
threads = []
supervisor_thread = None
data_lock = threading.Lock()

def format_duration(seconds):
    if seconds is None:
        return "N/A"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def connect_reader(reader_index):
    """Connect to a specific smart card reader by index."""
    try:
        reader_list = readers()
        mapped_index = READER_INDEX_MAPPING.get(reader_index, reader_index)
        if mapped_index >= len(reader_list):
            raise ValueError(f"No reader available for mapped index {mapped_index}")
        reader_name = reader_list[mapped_index].name
        logging.info(f"Thread {reader_index}: Connecting to reader: {reader_name} (sys {mapped_index} → logical {reader_index})")
        connection = reader_list[mapped_index].createConnection()
        observer = ConsoleCardConnectionObserver()
        connection.addObserver(observer)
        connection.connect()
        atr = toHexString(connection.getATR())
        logging.info(f"Thread {reader_index}: Connected (ATR: {atr})")
        return connection
    except Exception as e:
        logging.error(f"Thread {reader_index}: Reader connection error: {e}")
        return None

def execute_apdu(connection, apdu, thread_id):
    """Execute an APDU command and return response data and status."""
    try:
        data, sw1, sw2 = connection.transmit(toBytes(apdu))
        status = f"{sw1:02X}{sw2:02X}"
        logging.debug(f"Thread {thread_id}: APDU {apdu} → {toHexString(data)}, {status}")
        return toHexString(data).replace(" ", ""), status
    except Exception as e:
        logging.error(f"Thread {thread_id}: APDU exec error for {apdu}: {e}")
        return None, None

def create_socket(thread_id):
    """Create and connect a TCP socket to the server with retries."""
    start_time = time.time()
    while time.time() - start_time < SOCKET_RETRY_TIMEOUT and is_running:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            set_tcp_keepalive(sock, idle=60, interval=15, count=4)
            sock.settimeout(5)
            sock.connect((SERVER_IP, SERVER_PORT))
            set_tcp_keepalive(sock, idle=60, interval=15, count=4)
            logging.info(f"Thread {thread_id}: Connected to {SERVER_IP}:{SERVER_PORT} (keepalive ON)")
            return sock
        except socket.error as e:
            logging.warning(f"Thread {thread_id}: Socket connect error: {e}. Retrying in {SOCKET_RETRY_INTERVAL}s...")
            time.sleep(SOCKET_RETRY_INTERVAL)
    logging.error(f"Thread {thread_id}: Failed to connect after {SOCKET_RETRY_TIMEOUT}s")
    return None

def send_receive(sock, payload, operation, thread_id):
    """Send a payload to the server and receive the response (JSON)."""
    try:
        sock.sendall(payload)
        logging.debug(f"Thread {thread_id}: Sent {operation}: {payload.decode(errors='ignore')}")
        response = sock.recv(4096).decode(errors='ignore').strip()
        json_start = response.find('{')
        if json_start == -1:
            logging.error(f"Thread {thread_id}: No JSON in {operation} response: {response}")
            return None
        json_data = response[json_start:]
        try:
            return json.loads(json_data)
        except json.JSONDecodeError as e:
            logging.error(f"Thread {thread_id}: JSON parse error ({operation}): {e}, Raw: {json_data}")
            return None
    except (socket.timeout, TimeoutError) as e:
        logging.warning(f"Thread {thread_id}: Socket timeout during {operation}: {e}")
        return None
    except socket.error as e:
        logging.error(f"Thread {thread_id}: Socket error during {operation}: {e}")
        return None
    except Exception as e:
        logging.error(f"Thread {thread_id}: Unexpected error during {operation}: {e}")
        return None

# ------------------------------------------------------------------
# FIX: Proper send_identifier definition (the previous file was split)
# ------------------------------------------------------------------
def send_identifier(sock, identifier, thread_id, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "app_id": APP_ID, "device_id": DEVICE_ID, "reader_no": reader_no(thread_id)}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "atr", "data": payload_data}).encode()
    return send_receive(sock, payload, "send_identifier", thread_id)

# ------------------------------------------------------------------
# NEW: Helper to poll current auth status mid-auth
# ------------------------------------------------------------------
def get_current_auth_status(sock, identifier, thread_id, vehicle_schedule_id=None):
    """
    Poll the server for the current auth status of this card.
    Returns an int (0, 1, >1) or None on error.
    """
    try:
        resp = send_identifier(sock, identifier, thread_id, vehicle_schedule_id)
        if not resp:
            return None
        data = resp.get("data", {})
        if isinstance(data, dict):
            return data.get(identifier.lower(), None)
        return None
    except Exception as e:
        logging.error(f"Thread {thread_id}: Error polling auth status - {e}")
        return None

def fetch_company_name(sock, identifier, thread_id, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "app_id": APP_ID, "device_id": DEVICE_ID, "reader_no": reader_no(thread_id)}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "get_company_card", "data": payload_data}).encode()
    response_data = send_receive(sock, payload, "fetch_company_name", thread_id)
    if response_data and isinstance(response_data.get("data"), dict):
        company_name = response_data["data"].get(identifier.lower())
        if company_name:
            logging.info(f"Thread {thread_id}: Company name: {company_name}")
            return company_name
        logging.warning(f"Thread {thread_id}: No company name for ATR {identifier}, resp: {response_data}")
    else:
        logging.error(f"Thread {thread_id}: Invalid response from fetch_company_name: {response_data}")
    return None

def send_card_status(sock, identifier, thread_id, status, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "reader_no": reader_no(thread_id), "app_id": APP_ID, "device_id": DEVICE_ID}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": f"card_{status}", "data": payload_data}).encode()
    response_data = send_receive(sock, payload, f"send_card_status_{status}", thread_id)
    if response_data:
        logging.info(f"Thread {thread_id}: Card {status} sent OK: {response_data}")
    else:
        logging.error(f"Thread {thread_id}: Failed to send card {status}")
    return response_data

def fetch_apdu_from_server(sock, identifier, thread_id, response_data=None, status=None, pre_apdu=None, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "app_id": APP_ID, "device_id": DEVICE_ID, "reader_no": reader_no(thread_id)}
    if status is not None and response_data is not None:
        payload_data["response"] = response_data + status
        payload_data["apdu"] = pre_apdu
        if vehicle_schedule_id is not None:
            payload_data["vehicle_schedule_id"] = vehicle_schedule_id
        message_type = "response"
    else:
        message_type = "apdu"
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": message_type, "data": payload_data}).encode()
    response_data = send_receive(sock, payload, "fetch_apdu", thread_id)
    if response_data and isinstance(response_data.get("data"), dict):
        data_apdu = response_data["data"]
        if data_apdu:
            logging.debug(f"Thread {thread_id}: Received APDU: {data_apdu}")
            return data_apdu
        logging.error(f"Thread {thread_id}: No APDU received or invalid data")
    return None

history_data = []
HISTORY_LIMIT = 1000

def _append_history(entry):
    history_data.append(entry)
    if len(history_data) > HISTORY_LIMIT:
        history_data.pop(0)

def process_card(reader_index):
    """Process a single card in a separate thread; auto-recovers on errors."""
    thread_id = reader_index
    connection = None
    sock = None
    company_name = None
    vehicle_schedule_id = None
    has_reconnected = False
    prev_auth_status = -1
    combined_identifier = None
    connect_failures = 0
    last_card_ok = 0.0
    inserted_sent = False

    try:
        while is_running:
            # Ensure connection to reader
            if not connection:
                connection = connect_reader(reader_index)
                if not connection:
                    with data_lock:
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": "Disconnected",
                            "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                        })
                        rd = reader_data.get(reader_index)
                        if rd:
                            rd.update({"status": "Disconnected", "presentTime": "N/A",
                                       "cardInsertTime": None, "atr": "N/A", "companyName": "N/A",
                                       "authentication": "Unknown"})
                    # backoff after failed connect (T0/T1 unpowered, empty, etc.)
                    delay = min(MAX_CONNECT_BACKOFF, max(1.0, 0.5 * (2 ** connect_failures)))
                    connect_failures = min(connect_failures + 1, 6)
                    logging.debug(f"Thread {thread_id}: connect backoff {delay:.1f}s after failure {connect_failures}")
                    time.sleep(delay)
                    continue

                connect_failures = 0

                # Read identifier parts
                identifier_parts = []
                for i, apdu in enumerate(APDU_COMMANDS):
                    data, status = execute_apdu(connection, apdu, thread_id)
                    if data is None or status != "9000":
                        logging.error(f"Thread {thread_id}: APDU {apdu} failed with status: {status}")
                        with data_lock:
                            _append_history({
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index, "status": "Disconnected",
                                "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                            })
                            rd = reader_data.get(thread_id)
                            if rd:
                                rd.update({"status": "Disconnected", "presentTime": "N/A",
                                           "cardInsertTime": None, "companyName": "N/A"})
                        try:
                            connection.disconnect()
                        except Exception:
                            pass
                        connection = None
                        company_name = None
                        # backoff next connect try
                        delay = min(MAX_CONNECT_BACKOFF, max(1.0, 0.5 * (2 ** connect_failures)))
                        connect_failures = min(connect_failures + 1, 6)
                        logging.debug(f"Thread {thread_id}: connect backoff {delay:.1f}s after APDU select failure")
                        time.sleep(delay)
                        continue
                    if i in [1, 3]:
                        identifier_parts.append(data)

                if len(identifier_parts) != 2:
                    logging.error(f"Thread {thread_id}: Failed to collect both identifier parts")
                    with data_lock:
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": "Disconnected",
                            "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                        })
                        rd = reader_data.get(thread_id)
                        if rd:
                            rd.update({"status": "Disconnected", "presentTime": "N/A",
                                       "cardInsertTime": None, "companyName": "N/A"})
                    try:
                        connection.disconnect()
                    except Exception:
                        pass
                    connection = None
                    company_name = None
                    # backoff next connect try
                    delay = min(MAX_CONNECT_BACKOFF, max(1.0, 0.5 * (2 ** connect_failures)))
                    connect_failures = min(connect_failures + 1, 6)
                    logging.debug(f"Thread {thread_id}: connect backoff {delay:.1f}s after identifier failure")
                    time.sleep(delay)
                    continue

                combined_identifier = "".join(identifier_parts)
                last_card_ok = time.time()   # first successful access
                inserted_sent = False        # will send inserted below
                with data_lock:
                    _append_history({
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index, "status": "Connected",
                        "companyName": "N/A", "atr": combined_identifier,
                        "authentication": "Unknown", "presentTime": format_duration(0)
                    })
                    rd = reader_data.get(thread_id)
                    if rd:
                        rd.update({"status": "Connected", "atr": combined_identifier,
                                   "presentTime": format_duration(0), "cardInsertTime": time.time(),
                                   "companyName": "N/A"})

                sock = create_socket(thread_id)
                if not sock:
                    with data_lock:
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": "Disconnected",
                            "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                        })
                        rd = reader_data.get(thread_id)
                        if rd:
                            rd.update({"status": "Disconnected", "presentTime": "N/A",
                                       "cardInsertTime": None, "companyName": "N/A"})
                    try:
                        connection.disconnect()
                    except Exception:
                        pass
                    connection = None
                    company_name = None
                    delay = min(MAX_CONNECT_BACKOFF, max(1.0, 0.5 * (2 ** connect_failures)))
                    connect_failures = min(connect_failures + 1, 6)
                    logging.debug(f"Thread {thread_id}: connect backoff {delay:.1f}s after socket failure")
                    time.sleep(delay)
                    continue

                # Send card inserted once per cycle
                if not inserted_sent:
                    send_card_status(sock, combined_identifier, thread_id, "inserted", vehicle_schedule_id)
                    inserted_sent = True

            # Update present time
            with data_lock:
                rd = reader_data.get(thread_id)
                if rd and rd.get("cardInsertTime"):
                    rd["presentTime"] = format_duration(time.time() - rd["cardInsertTime"])
                    _append_history({
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index, "status": rd["status"],
                        "companyName": rd["companyName"], "atr": rd["atr"],
                        "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                    })
                elif rd:
                    rd["presentTime"] = "N/A"

            try:
                connection.getATR()
                last_card_ok = time.time()
            except Exception as e:
                # Only declare removal if we've had no successful ATR for > grace period
                if (time.time() - last_card_ok) >= REMOVAL_GRACE_SEC:
                    logging.error(f"Thread {thread_id}: getATR failed (card removed?): {e}")
                    if sock and combined_identifier and inserted_sent:
                        send_card_status(sock, combined_identifier, thread_id, "removed", vehicle_schedule_id)
                    with data_lock:
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": "Card Removed",
                            "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                        })
                        rd = reader_data.get(thread_id)
                        if rd:
                            rd.update({"status": "Card Removed", "presentTime": "N/A",
                                       "cardInsertTime": None, "atr": "N/A",
                                       "authentication": "Unknown", "companyName": "N/A"})
                    try:
                        connection.disconnect()
                    except Exception:
                        pass
                    connection = None
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None
                    company_name = None
                    combined_identifier = None
                    vehicle_schedule_id = None
                    inserted_sent = False
                    connect_failures = 0  # reset; next connect shouldn’t be delayed
                else:
                    # transient blip; don't mark removed, just wait a bit
                    logging.debug(f"Thread {thread_id}: transient ATR error within grace; not removing yet")
                time.sleep(REQUEST_INTERVAL)
                continue

            # Query server for auth decision
            response_data = send_identifier(sock, combined_identifier, thread_id, vehicle_schedule_id)
            if not response_data:
                logging.error(f"Thread {thread_id}: No server response, reconnecting socket…")
                try:
                    sock.close()
                except Exception:
                    pass
                sock = create_socket(thread_id)
                if not sock:
                    with data_lock:
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": "Disconnected",
                            "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                        })
                        rd = reader_data.get(thread_id)
                        if rd:
                            rd.update({"status": "Disconnected", "presentTime": "N/A",
                                       "cardInsertTime": None, "companyName": "N/A"})
                    try:
                        connection.disconnect()
                    except Exception:
                        pass
                    connection = None
                    company_name = None
                    combined_identifier = None
                    inserted_sent = False
                    # small delay before trying again
                    time.sleep(REQUEST_INTERVAL)
                    continue
                time.sleep(REQUEST_INTERVAL)
                continue

            if isinstance(response_data.get("data"), dict):
                vehicle_schedule_id = response_data["data"].get("vehicle_schedule_id")

            auth_status = response_data.get("data", {}).get(combined_identifier.lower(), -1)
            with data_lock:
                rd = reader_data.get(thread_id)
                if rd:
                    rd["authentication"] = (
                        "No Authentication Required" if auth_status == 0 else
                        "Authentication Required" if auth_status == 1 else
                        f"Authentication Failed ({auth_status})" if auth_status > 1 else
                        "Unknown"
                    )
                    _append_history({
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index, "status": rd["status"],
                        "companyName": rd["companyName"], "atr": rd["atr"],
                        "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                    })

            if auth_status == 1:
                if auth_status != prev_auth_status:
                    logging.info(f"Thread {thread_id}: Authentication required")
                with data_lock:
                    rd = reader_data.get(thread_id)
                    if rd:
                        rd["status"] = "Connected"
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": rd["status"],
                            "companyName": rd["companyName"], "atr": rd["atr"],
                            "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                        })
                data_apdu = fetch_apdu_from_server(sock, combined_identifier, thread_id, vehicle_schedule_id=vehicle_schedule_id)
                if not data_apdu:
                    logging.error(f"Thread {thread_id}: Failed to fetch APDU")
                    time.sleep(REQUEST_INTERVAL)
                    continue
                apdu = data_apdu.get('apdu')
                vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)

                while apdu and apdu != "00000000000000":
                    # NEW: Check if authentication got cancelled mid-loop
                    polled_status = get_current_auth_status(sock, combined_identifier, thread_id, vehicle_schedule_id)
                    if polled_status == 0:
                        logging.info(f"Thread {thread_id}: Auth status switched to 0 mid-auth → exiting APDU loop")
                        break

                    if apdu == "11111111111111":
                        logging.debug(f"Thread {thread_id}: Skipping APDU {apdu}")
                        data_apdu = fetch_apdu_from_server(sock, combined_identifier, thread_id, vehicle_schedule_id=vehicle_schedule_id)
                        if not data_apdu:
                            break
                        apdu = data_apdu.get('apdu')
                        vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)
                        continue

                    data, status = execute_apdu(connection, apdu, thread_id)
                    if data is None or status is None:
                        logging.error(f"Thread {thread_id}: Auth APDU {apdu} failed")
                        with data_lock:
                            _append_history({
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index, "status": "Disconnected",
                                "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                            })
                            rd = reader_data.get(thread_id)
                            if rd:
                                rd.update({"status": "Disconnected", "presentTime": "N/A",
                                           "cardInsertTime": None, "CompanyName": "N/A"})
                        try:
                            connection.disconnect()
                        except Exception:
                            pass
                        connection = None
                        company_name = None
                        combined_identifier = None
                        inserted_sent = False
                        # Recover; do not kill thread
                        break

                    data_apdu = fetch_apdu_from_server(
                        sock, combined_identifier, thread_id,
                        response_data=data, status=status, pre_apdu=apdu,
                        vehicle_schedule_id=vehicle_schedule_id
                    )
                    if not data_apdu:
                        break
                    apdu = data_apdu.get('apdu')
                    vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)

                if apdu == "00000000000000":
                    data, status = execute_apdu(connection, apdu, thread_id)
                    if data is None or status is None:
                        logging.error(f"Thread {thread_id}: Final APDU {apdu} failed")
                    else:
                        logging.info(f"Thread {thread_id}: Authentication complete")
                        with data_lock:
                            _append_history({
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index, "status": "Connected",
                                "companyName": "N/A", "atr": reader_data.get(thread_id, {}).get("atr", "N/A"),
                                "authentication": "No Authentication Required", "presentTime": format_duration(0)
                            })
                            rd = reader_data.get(thread_id)
                            if rd:
                                rd.update({"status": "Connected", "authentication": "No Authentication Required",
                                           "cardInsertTime": time.time(), "presentTime": format_duration(0),
                                           "companyName": "N/A"})
                        try:
                            connection.disconnect()
                        except Exception:
                            pass
                        connection = connect_reader(reader_index)
                        if not connection:
                            with data_lock:
                                _append_history({
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                    "readerIndex": reader_index, "status": "Disconnected",
                                    "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                                })
                                rd = reader_data.get(thread_id)
                                if rd:
                                    rd.update({"status": "Disconnected", "presentTime": "N/A",
                                               "cardInsertTime": None, "companyName": "N/A"})
                            company_name = None
                            combined_identifier = None
                            inserted_sent = False
                            # Recover loop
                            continue
                        has_reconnected = True
                        last_card_ok = time.time()
                        # Do NOT send removed here; the card is still present

            elif auth_status == 0:
                if auth_status != prev_auth_status:
                    logging.info(f"Thread {thread_id}: No authentication required")
                with data_lock:
                    rd = reader_data.get(thread_id)
                    if rd:
                        rd["status"] = "Card-Connected"
                        if not rd.get("cardInsertTime"):
                            rd["cardInsertTime"] = time.time()
                            rd["presentTime"] = format_duration(0)
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": rd["status"],
                            "companyName": rd["companyName"], "atr": rd["atr"],
                            "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                        })
                company_name = fetch_company_name(sock, combined_identifier, thread_id, vehicle_schedule_id)
                with data_lock:
                    rd = reader_data.get(thread_id)
                    if rd:
                        rd["companyName"] = company_name if company_name else "N/A"
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": rd["status"],
                            "companyName": rd["companyName"], "atr": rd["atr"],
                            "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                        })
                has_reconnected = False

            elif auth_status > 1 and not has_reconnected and prev_auth_status <= 1:
                logging.info(f"Thread {thread_id}: Auth status {auth_status}>1 → reconnect reader")
                with data_lock:
                    _append_history({
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index, "status": "Disconnected",
                        "companyName": "N/A", "atr": "N/A",
                        "authentication": reader_data.get(thread_id, {}).get("authentication", "Unknown"),
                        "presentTime": "N/A"
                    })
                    rd = reader_data.get(thread_id)
                    if rd:
                        rd.update({"status": "Disconnected", "presentTime": "N/A",
                                   "cardInsertTime": None, "companyName": "N/A"})
                try:
                    connection.disconnect()
                except Exception:
                    pass
                connection = connect_reader(reader_index)
                if not connection:
                    with data_lock:
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": "Disconnected",
                            "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
                        })
                        rd = reader_data.get(thread_id)
                        if rd:
                            rd.update({"status": "Disconnected", "presentTime": "N/A",
                                       "cardInsertTime": None, "companyName": "N/A"})
                    company_name = None
                    combined_identifier = None
                    inserted_sent = False
                    delay = min(MAX_CONNECT_BACKOFF, max(1.0, 0.5 * (2 ** connect_failures)))
                    connect_failures = min(connect_failures + 1, 6)
                    logging.debug(f"Thread {thread_id}: connect backoff {delay:.1f}s after reconnect failure")
                    time.sleep(delay)
                    continue
                has_reconnected = True
                last_card_ok = time.time()

            elif auth_status > 1 and has_reconnected:
                logging.debug(f"Thread {thread_id}: Auth {auth_status}>1, already reconnected")
            elif auth_status > 1 and prev_auth_status > 1:
                logging.debug(f"Thread {thread_id}: Auth {auth_status}>1, no status change")
            else:
                with data_lock:
                    rd = reader_data.get(thread_id)
                    if rd:
                        rd["authentication"] = "Unknown"
                        _append_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index, "status": rd["status"],
                            "companyName": rd["companyName"], "atr": rd["atr"],
                            "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                        })
                has_reconnected = False

            prev_auth_status = auth_status
            time.sleep(REQUEST_INTERVAL)

    except Exception as e:
        logging.error(f"Thread {thread_id}: Unexpected error: {e}")
        with data_lock:
            _append_history({
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                "readerIndex": reader_index, "status": "Disconnected",
                "companyName": "N/A", "atr": "N/A", "authentication": "Unknown", "presentTime": "N/A"
            })
            rd = reader_data.get(thread_id)
            if rd:
                rd.update({"status": "Disconnected", "presentTime": "N/A",
                           "cardInsertTime": None, "companyName": "N/A"})
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
            logging.info(f"Thread {thread_id}: Socket closed")
        if connection:
            try:
                connection.disconnect()
                logging.info(f"Thread {thread_id}: Reader disconnected")
            except Exception as e:
                logging.error(f"Thread {thread_id}: Error disconnecting reader: {e}")

def supervise_threads():
    """Restart any worker thread that dies (belt-and-suspenders)."""
    global threads
    while is_running:
        try:
            with data_lock:
                snapshot = list(threads)
            for idx, t in enumerate(snapshot):
                if t is None:
                    continue
                if not t.is_alive() and is_running:
                    logging.warning(f"Supervisor: restarting reader thread {idx}")
                    nt = threading.Thread(target=process_card, args=(idx,), daemon=True)
                    with data_lock:
                        threads[idx] = nt
                    nt.start()
            time.sleep(5)
        except Exception as e:
            logging.error(f"Supervisor error: {e}")
            time.sleep(5)

def _init_reader_data(n):
    global reader_data
    reader_data = {
        i: {
            "readerIndex": i,
            "status": "Disconnected",
            "companyName": "N/A",
            "atr": "N/A",
            "authentication": "Unknown",
            "presentTime": "N/A",
            "cardInsertTime": None
        } for i in range(n)
    }

def start_processing():
    global is_running, threads, READER_INDEX_MAPPING, supervisor_thread
    with data_lock:
        if is_running:
            return True
        is_running = True

    try:
        system_readers = readers()
        count = len(system_readers)
        if count == 0:
            logging.error("No smart-card readers detected.")
            with data_lock:
                is_running = False
            return False

        with data_lock:
            READER_INDEX_MAPPING = {i: i for i in range(count)}
            _init_reader_data(count)
            threads = []

        # Start worker threads (daemon)
        for i in range(count):
            t = threading.Thread(target=process_card, args=(i,), daemon=True)
            with data_lock:
                threads.append(t)
            t.start()

        # Start supervisor
        supervisor_thread = threading.Thread(target=supervise_threads, daemon=True)
        supervisor_thread.start()

        logging.info(f"Started processing for {count} reader(s) at {time.strftime('%H:%M:%S', time.localtime())}")
        return True

    except Exception as e:
        logging.error(f"Failed to start processing at {time.strftime('%H:%M:%S', time.localtime())}: {e}")
        with data_lock:
            is_running = False
            threads = []
        return False

def stop_processing():
    global is_running, threads
    # Flip the flag without holding lock during join
    with data_lock:
        if not is_running:
            return
        is_running = False
        local_threads = list(threads)

    # Join outside the lock to avoid deadlocks
    for t in local_threads:
        try:
            if t:
                t.join(timeout=5)
        except Exception:
            pass

    with data_lock:
        threads = []
        # Reset state
        for i in list(reader_data.keys()):
            reader_data[i] = {
                "readerIndex": i,
                "status": "Disconnected",
                "companyName": "N/A",
                "atr": "N/A",
                "authentication": "Unknown",
                "presentTime": "N/A",
                "cardInsertTime": None
            }
    logging.info(f"Stopped processing for all readers at {time.strftime('%H:%M:%S', time.localtime())}")

# Flask routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/readers')
def get_readers():
    try:
        with data_lock:
            data = [{k: v for k, v in reader.items() if k != "cardInsertTime"} for reader in reader_data.values()]
            logging.debug(f"Serving reader data at {time.strftime('%H:%M:%S', time.localtime())}: {data}")
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        logging.error(f"Error in get_readers: {e}")
        return jsonify({"status": "error", "message": str(e), "data": []}), 500

@app.route('/history')
def get_history():
    try:
        reader_index = request.args.get('readerIndex', type=int)
        with data_lock:
            if reader_index is not None:
                filtered = [entry for entry in history_data if entry.get('readerIndex') == reader_index]
                return jsonify({"status": "success", "data": filtered})
            else:
                return jsonify({"status": "success", "data": history_data})
    except Exception as e:
        logging.error(f"Error in get_history: {e}")
        return jsonify({"status": "error", "message": str(e), "data": []}), 500

@app.route('/download_history')
def download_history():
    try:
        with data_lock:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=["timestamp", "readerIndex", "status", "companyName", "atr", "authentication", "presentTime"])
            writer.writeheader()
            for entry in history_data:
                writer.writerow(entry)
            output.seek(0)
            return send_file(
                io.BytesIO(output.getvalue().encode('utf-8')),
                mimetype='text/csv',
                as_attachment=True,
                download_name='history.csv'
            )
    except Exception as e:
        logging.error(f"Error in download_history: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        logging.debug(f"Received login request at {time.strftime('%H:%M:%S', time.localtime())}")
        data = request.get_json()
        if data is None:
            logging.error("Login failed: No JSON data received")
            return jsonify({"status": "error", "message": "Request must be application/json"}), 415

        username = data.get('username')
        password = data.get('password')

        # Static credentials
        if username == 'techvezoto' and password == 'techvezoto@1122':
            if start_processing():
                logging.info("Login successful; processing started")
                return jsonify({"status": "success"})
            else:
                logging.error("Login failed: processing could not start")
                return jsonify({"status": "error", "message": "Failed to start reader processing"}), 500

        logging.warning("Login failed: Invalid credentials")
        return jsonify({"status": "error", "message": "Invalid username or password"}), 401

    except Exception as e:
        logging.error(f"Error in login: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/logout', methods=['POST'])
def logout():
    try:
        stop_processing()
        logging.info("Logout successful; processing stopped")
        return jsonify({"status": "success"})
    except Exception as e:
        logging.error(f"Error in logout: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/graphs.html')
def graphs():
    return render_template('graphs.html')

@app.route('/logs.html')
def logs():
    return render_template('logs.html')

@socketio.on('connect', namespace='/logs')
def handle_connect():
    logging.info(f"Client connected to /logs at {time.strftime('%H:%M:%S', time.localtime())}")

@socketio.on('disconnect', namespace='/logs')
def handle_disconnect():
    logging.info(f"Client disconnected from /logs at {time.strftime('%H:%M:%S', time.localtime())}")

if __name__ == "__main__":
    socketio.run(app, debug=False, host='0.0.0.0', port=WEB_PORT, allow_unsafe_werkzeug=True)
