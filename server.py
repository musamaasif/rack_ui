from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO
import socket
import sys
import json, io, csv
import time
import threading
import logging
import glob
import os
from logging.handlers import RotatingFileHandler
from smartcard.System import readers
from smartcard.util import toHexString, toBytes
from smartcard.CardConnectionObserver import ConsoleCardConnectionObserver
import smtplib, ssl
from email.message import EmailMessage

# MANUAL READER ORDER (New RACK)
MANUAL_SERIAL_ORDER = [
    "OMNIKEY AG CardMan 3121 00 00",                                                    # Slot 1
    "OMNIKEY AG CardMan 3121 01 00",                                                    # Slot 2
    "HID Global OMNIKEY 3x21 Smart Card Reader [OMNIKEY 3x21 Smart Card Reader] 02 00", # Slot 3
    "OMNIKEY AG CardMan 3121 03 00",                                                    # Slot 4
    "HID Global OMNIKEY 3x21 Smart Card Reader [OMNIKEY 3x21 Smart Card Reader] 0C 00", # Slot 5
    "OMNIKEY AG CardMan 3121 0D 00",                                                    # Slot 6
    "HID Global OMNIKEY 3x21 Smart Card Reader [OMNIKEY 3x21 Smart Card Reader] 0E 00", # Slot 7
    "OMNIKEY AG CardMan 3121 0F 00",                                                    # Slot 8
    "OMNIKEY AG CardMan 3121 08 00",                                                    # Slot 9
    "OMNIKEY AG CardMan 3121 09 00",                                                    # Slot 10
    "OMNIKEY AG CardMan 3121 0A 00",                                                    # Slot 11
    "OMNIKEY AG CardMan 3121 0B 00",                                                    # Slot 12
    "OMNIKEY AG CardMan 3121 04 00",                                                    # Slot 13
    "OMNIKEY AG CardMan 3121 05 00",                                                    # Slot 14
    "HID Global OMNIKEY 3x21 Smart Card Reader [OMNIKEY 3x21 Smart Card Reader] 06 00", # Slot 15
    "HID Global OMNIKEY 3x21 Smart Card Reader [OMNIKEY 3x21 Smart Card Reader] 07 00"  # Slot 16
]

SORTED_READERS_CACHE = []

def get_readers_mapped():
    """
    Returns readers sorted exactly according to MANUAL_SERIAL_ORDER.
    Supports both Full Names (OMNIKEY) and Serial Numbers (Identive).
    """
    try:
        pcsc_list = readers()
        if not pcsc_list: return []

        available_readers_by_name = {}
        available_readers_by_serial = {}
        
        for r in pcsc_list:
            r_name = str(r)
            available_readers_by_name[r_name] = r 
            try:
                if "(" in r_name:
                    serial = r_name.split("(")[1].split(")")[0]
                    available_readers_by_serial[serial] = r
            except: 
                pass

        final_list = []
        for identifier in MANUAL_SERIAL_ORDER:
            if identifier in available_readers_by_name:
                final_list.append(available_readers_by_name[identifier])
                del available_readers_by_name[identifier]
            elif identifier in available_readers_by_serial:
                final_list.append(available_readers_by_serial[identifier])
                del available_readers_by_serial[identifier]
            else:
                pass 

        for r_name, r in available_readers_by_name.items():
            if r not in final_list:
                final_list.append(r)

        logging.info(f"[MAPPING] Sorted {len(final_list)} readers based on Manual Configuration.")
        return final_list

    except Exception as e:
        logging.error(f"[MAPPING FAILED] {e}. Falling back to system order.")
        try:
            return readers()
        except:
            return []

def set_tcp_keepalive(sock: socket.socket, *, idle=60, interval=15, count=4):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform.startswith("linux"):
            TCP_KEEPIDLE = getattr(socket, "TCP_KEEPIDLE", 4)
            TCP_KEEPINTVL = getattr(socket, "TCP_KEEPINTVL", 5)
            TCP_KEEPCNT = getattr(socket, "TCP_KEEPCNT", 6)
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPIDLE, idle)
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPINTVL, interval)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
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

class SocketIOHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            socketio.emit('log_message', {'data': msg}, namespace='/logs')
        except Exception as e:
            print(f"SocketIOHandler error: {e}")

logging.getLogger('').setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(formatter)

socketio_handler = SocketIOHandler()
socketio_handler.setLevel(logging.DEBUG)
socketio_handler.setFormatter(formatter)

logging.getLogger('').addHandler(console_handler)
logging.getLogger('').addHandler(socketio_handler)

SERVER_IP = "206.189.24.200"
SERVER_PORT = 20119
APP_ID = "r08"
DEVICE_ID = "rack16"
WEB_PORT = 5000
DEVICE_OFFSET = 0

EMAIL_SMTP_HOST = "vmi1602170.contaboserver.net"
EMAIL_SMTP_PORT = 587
EMAIL_USERNAME = "cardrackservices@tecnorn.online"
EMAIL_PASSWORD = "ICV?U-%y.mit"
EMAIL_TO_LIST = ["m.usaamaasif@gmail.com", "usamamughal8345@gmail.com"]

AUTH_TIMEOUT_SEC = 60

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
initial_reader_count = 0

def format_duration(seconds):
    if seconds is None:
        return "N/A"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def send_email(subject, body):
    msg = EmailMessage()
    msg['Subject'] = f"[{DEVICE_ID}] {subject}"
    msg['From'] = EMAIL_USERNAME
    msg['To'] = ", ".join(EMAIL_TO_LIST)
    msg.set_content(body)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.send_message(msg)
            logging.info(f"Email sent successfully: {subject}")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

def send_email_async(subject, body):
    mail_thread = threading.Thread(target=send_email, args=(subject, body), daemon=True)
    mail_thread.start()

def hourly_heartbeat():
    logging.info("Hourly heartbeat thread started.")
    while is_running:
        try:
            for _ in range(28800):
                if not is_running: break
                time.sleep(1)
            
            if is_running:
                logging.info("Sending hourly status email...")
                send_email_async(
                    f"Hourly Status: {DEVICE_ID} is ON",
                    f"This is an automated hourly check-in.\n"
                    f"The application {APP_ID} on device {DEVICE_ID} is running."
                )
        except Exception as e:
            logging.error(f"Heartbeat thread error: {e}")
            time.sleep(300)

def connect_reader(reader_index):
    try:
        global SORTED_READERS_CACHE
        if not SORTED_READERS_CACHE:
            SORTED_READERS_CACHE = get_readers_mapped() 
            
        reader_list = SORTED_READERS_CACHE

        mapped_index = READER_INDEX_MAPPING.get(reader_index, reader_index)
        if mapped_index >= len(reader_list):
            raise ValueError(f"No reader available for mapped index {mapped_index} (System count: {len(reader_list)})")
        reader_name = reader_list[mapped_index].name
        logging.info(f"Thread {reader_index}: Connecting to reader: {reader_name} (sys {mapped_index} -> logical {reader_index})")
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
    try:
        data, sw1, sw2 = connection.transmit(toBytes(apdu))
        status = f"{sw1:02X}{sw2:02X}"
        logging.debug(f"Thread {thread_id}: APDU {apdu} -> {toHexString(data)}, {status}")
        return toHexString(data).replace(" ", ""), status
    except Exception as e:
        logging.error(f"Thread {thread_id}: APDU exec error for {apdu}: {e}")
        return None, None

def create_socket(thread_id):
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

def send_identifier(sock, identifier, thread_id, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "app_id": APP_ID, "device_id": DEVICE_ID, "reader_no": reader_no(thread_id)}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "atr", "data": payload_data}).encode()
    return send_receive(sock, payload, "send_identifier", thread_id)

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
    completed_vsid = None   # last vehicle_schedule_id successfully acknowledged with 9000
    has_reconnected = False
    prev_auth_status = -1
    combined_identifier = None
    connect_failures = 0
    last_card_ok = 0.0
    inserted_sent = False
    auth_start_time = 0

    try:
        while is_running:
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
                    delay = min(MAX_CONNECT_BACKOFF, max(1.0, 0.5 * (2 ** connect_failures)))
                    connect_failures = min(connect_failures + 1, 6)
                    logging.debug(f"Thread {thread_id}: connect backoff {delay:.1f}s after failure {connect_failures}")
                    time.sleep(delay)
                    continue

                connect_failures = 0
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
                    delay = min(MAX_CONNECT_BACKOFF, max(1.0, 0.5 * (2 ** connect_failures)))
                    connect_failures = min(connect_failures + 1, 6)
                    logging.debug(f"Thread {thread_id}: connect backoff {delay:.1f}s after identifier failure")
                    time.sleep(delay)
                    continue

                combined_identifier = "".join(identifier_parts)
                completed_vsid = None    # fresh card session — forget any previous vsid
                last_card_ok = time.time()   
                inserted_sent = False        
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
                                   "companyName": "N/A",
                                   "unknown_email_sent": False}) 

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

                if not inserted_sent:
                    send_card_status(sock, combined_identifier, thread_id, "inserted", vehicle_schedule_id)
                    inserted_sent = True

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
                if (time.time() - last_card_ok) >= REMOVAL_GRACE_SEC:
                    logging.error(f"Thread {thread_id}: getATR failed (card removed?): {e}")
                    if sock and combined_identifier and inserted_sent:
                        send_card_status(sock, combined_identifier, thread_id, "removed", vehicle_schedule_id)
                        send_email_async(
                            f"Card Removed from Reader {reader_no(thread_id)}",
                            f"The card (ATR: {combined_identifier}) was removed from reader {reader_no(thread_id)}."
                        )
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
                                       "authentication": "Unknown", "companyName": "N/A",
                                       "unknown_email_sent": False}) 
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
                    auth_start_time = 0        
                    inserted_sent = False
                    connect_failures = 0 
                else:
                    logging.debug(f"Thread {thread_id}: transient ATR error within grace; not removing yet")
                time.sleep(REQUEST_INTERVAL)
                continue

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
                    vehicle_schedule_id = None 
                    auth_start_time = 0        
                    inserted_sent = False
                    time.sleep(REQUEST_INTERVAL) 
                    continue
                time.sleep(REQUEST_INTERVAL)
                continue

            if isinstance(response_data.get("data"), dict):
                new_sid = response_data["data"].get("vehicle_schedule_id")
                if new_sid != vehicle_schedule_id:
                    vehicle_schedule_id = new_sid
                    auth_start_time = time.time()

            raw_status = response_data.get("data", {}).get(combined_identifier.lower(), -1)
            try:
                auth_status = int(raw_status)
            except (ValueError, TypeError):
                auth_status = -1

            # ── Dedup guard ───────────────────────────────────────────────────
            # The server permanently stores vehicle schedules and keeps returning
            # auth_status=1 for the same vehicle_schedule_id after every reconnect.
            # Once we have successfully acknowledged a vsid with 9000, suppress
            # re-authentication for that same vsid and treat the card as
            # Card-Connected. completed_vsid is reset on fresh card insertion so
            # a genuinely new vsid always triggers a full auth cycle.
            if auth_status == 1 and vehicle_schedule_id is not None and vehicle_schedule_id == completed_vsid:
                logging.debug(f"Thread {thread_id}: vsid {vehicle_schedule_id} already completed — suppressing re-auth.")
                auth_status = 0
            # ─────────────────────────────────────────────────────────────────

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

                if company_name is None:
                    company_name = fetch_company_name(
                        sock, combined_identifier, thread_id, vehicle_schedule_id
                    )
                    if company_name:
                        with data_lock:
                            rd = reader_data.get(thread_id)
                            if rd:
                                rd["companyName"] = company_name
                                _append_history({
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                    "readerIndex": reader_index,
                                    "status": rd["status"],
                                    "companyName": rd["companyName"],
                                    "atr": rd["atr"],
                                    "authentication": rd["authentication"],
                                    "presentTime": rd["presentTime"],
                                })
                    else:
                        with data_lock:
                            rd = reader_data.get(thread_id)
                            if rd and not rd.get("unknown_email_sent"):
                                send_email_async(
                                    f"Unknown Card on Reader {reader_no(thread_id)}",
                                    f"A card (ATR: {combined_identifier}) was inserted in reader {reader_no(thread_id)}, "
                                    f"but its company name is 'N/A'."
                                )
                                rd["unknown_email_sent"] = True

                data_apdu = fetch_apdu_from_server(sock, combined_identifier, thread_id, vehicle_schedule_id=vehicle_schedule_id)
                if not data_apdu:
                    logging.error(f"Thread {thread_id}: Failed to fetch APDU")
                    time.sleep(REQUEST_INTERVAL)
                    continue
                apdu = data_apdu.get('apdu')
                apdu_auth_status = int(data_apdu.get('auth_status', 1))
                vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)

                while apdu or apdu_auth_status == 1:
                    if auth_start_time > 0 and (time.time() - auth_start_time) > AUTH_TIMEOUT_SEC:
                        logging.error(f"Thread {thread_id}: Auth timed out (> {AUTH_TIMEOUT_SEC}s), forcefully resetting session.")
                        if sock and combined_identifier:
                            send_card_status(sock, combined_identifier, thread_id, "removed", vehicle_schedule_id)
                        
                        vehicle_schedule_id = None 
                        auth_start_time = 0
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
                        break

                    # ── Auth-complete via auth_status=0 ───────────────────────
                    # Server signals all APDUs are done by returning auth_status=0
                    # in an APDU response. The card rack sends 11111111111111 to
                    # acknowledge, then reconnects. This replaces the old model
                    # where the server sent "11111111111111" as an APDU, which
                    # was prone to races when the server was slow.
                    if apdu_auth_status == 0:
                        logging.info(f"Thread {thread_id}: auth_status=0 received — authentication complete.")
                        fetch_apdu_from_server(
                            sock, combined_identifier, thread_id,
                            response_data="", status="9000", pre_apdu="11111111111111",
                            vehicle_schedule_id=vehicle_schedule_id
                        )
                        with data_lock:
                            _append_history({
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index, "status": "Card-Connected",
                                "companyName": company_name if company_name else "N/A",
                                "atr": reader_data.get(thread_id, {}).get("atr", "N/A"),
                                "authentication": "No Authentication Required",
                                "presentTime": reader_data.get(thread_id, {}).get("presentTime", "N/A")
                            })
                            rd = reader_data.get(thread_id)
                            if rd:
                                rd.update({
                                    "status": "Card-Connected",
                                    "authentication": "No Authentication Required",
                                    "companyName": company_name if company_name else rd.get("companyName", "N/A")
                                })
                        completed_vsid = vehicle_schedule_id
                        vehicle_schedule_id = None
                        auth_start_time = 0
                        try:
                            connection.disconnect()
                        except Exception:
                            pass
                        connection = connect_reader(reader_index)
                        if not connection:
                            company_name = None
                            combined_identifier = None
                            inserted_sent = False
                            break
                        _ok, _parts = True, []
                        for _i, _cmd in enumerate(APDU_COMMANDS):
                            _d, _s = execute_apdu(connection, _cmd, thread_id)
                            if _d is None or _s != "9000":
                                logging.warning(f"Thread {thread_id}: Re-init {_cmd} failed ({_s}) after auth.")
                                _ok = False
                                break
                            if _i in [1, 3]:
                                _parts.append(_d)
                        if _ok and len(_parts) == 2:
                            combined_identifier = "".join(_parts)
                            logging.info(f"Thread {thread_id}: Card context restored after auth complete.")
                        else:
                            logging.error(f"Thread {thread_id}: Re-init failed after auth — treating as removal.")
                            try: connection.disconnect()
                            except Exception: pass
                            connection = None
                            company_name = None
                            combined_identifier = None
                            inserted_sent = False
                            break
                        has_reconnected = True
                        last_card_ok = time.time()
                        break
                    # ─────────────────────────────────────────────────────────

                    # If server has no apdu for us right now but auth is still
                    # pending (auth_status=1, apdu=None/empty), poll briefly.
                    if not apdu:
                        logging.debug(f"Thread {thread_id}: auth_status=1 but no apdu yet — polling.")
                        time.sleep(REQUEST_INTERVAL)
                        data_apdu = fetch_apdu_from_server(sock, combined_identifier, thread_id, vehicle_schedule_id=vehicle_schedule_id)
                        if not data_apdu:
                            break
                        apdu = data_apdu.get('apdu')
                        apdu_auth_status = int(data_apdu.get('auth_status', 1))
                        vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)
                        continue
                        
                    if apdu == "11111111111111":
                        if apdu_auth_status == 1:
                            # Server still has auth_status=1 — more APDUs are coming.
                            # Acknowledge this APDU with 9000 and keep waiting.
                            logging.info(f"Thread {thread_id}: 11111111111111 received but auth_status=1 — acknowledging and waiting for next APDU.")
                            data_apdu = fetch_apdu_from_server(
                                sock, combined_identifier, thread_id,
                                response_data="", status="9000", pre_apdu=apdu,
                                vehicle_schedule_id=vehicle_schedule_id
                            )
                            if not data_apdu:
                                logging.error(f"Thread {thread_id}: No APDU after 11111111111111 mid-auth ack")
                                break
                            apdu = data_apdu.get('apdu')
                            apdu_auth_status = int(data_apdu.get('auth_status', 1))
                            vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)
                            continue

                        # auth_status=0 AND apdu=11111111111111 — both conditions met.
                        logging.info(f"Thread {thread_id}: Authentication COMPLETE (11111111111111 + auth_status=0)")
                        
                        # Acknowledge the dummy APDU to close the server's schedule
                        fetch_apdu_from_server(
                            sock, combined_identifier, thread_id,
                            response_data="", status="9000", pre_apdu=apdu,
                            vehicle_schedule_id=vehicle_schedule_id
                        )

                        with data_lock:
                            _append_history({
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index, "status": "Card-Connected",
                                "companyName": company_name if company_name else "N/A",
                                "atr": reader_data.get(thread_id, {}).get("atr", "N/A"),
                                "authentication": "No Authentication Required",
                                "presentTime": reader_data.get(thread_id, {}).get("presentTime", "N/A")
                            })
                            rd = reader_data.get(thread_id)
                            if rd:
                                rd.update({
                                    "status": "Card-Connected",
                                    "authentication": "No Authentication Required",
                                    "companyName": company_name if company_name else rd.get("companyName", "N/A")
                                })

                        # Remember this vsid BEFORE clearing it so the dedup guard can
                        # suppress repeat auth requests for the same schedule.
                        completed_vsid = vehicle_schedule_id
                        vehicle_schedule_id = None
                        auth_start_time = 0

                        # Reconnect is required: it signals the server that the card
                        # is freshly presented, causing it to return auth_status=0
                        # instead of re-issuing auth_status=1 on the next poll.
                        try:
                            connection.disconnect()
                        except Exception:
                            pass

                        connection = connect_reader(reader_index)
                        if not connection:
                            company_name = None
                            combined_identifier = None
                            inserted_sent = False
                            break

                        # Re-run APDU_COMMANDS after cold reconnect to restore the
                        # card's EF selection context.  Without this, subsequent auth
                        # APDUs return SW 6986 ("no current EF selected").
                        _ok, _parts = True, []
                        for _i, _cmd in enumerate(APDU_COMMANDS):
                            _d, _s = execute_apdu(connection, _cmd, thread_id)
                            if _d is None or _s != "9000":
                                logging.warning(f"Thread {thread_id}: Re-init {_cmd} failed ({_s}) after auth.")
                                _ok = False
                                break
                            if _i in [1, 3]:
                                _parts.append(_d)
                        if _ok and len(_parts) == 2:
                            combined_identifier = "".join(_parts)
                            logging.info(f"Thread {thread_id}: Card context restored after auth complete.")
                        else:
                            logging.error(f"Thread {thread_id}: Re-init failed after auth — treating as removal.")
                            try: connection.disconnect()
                            except Exception: pass
                            connection = None
                            company_name = None
                            combined_identifier = None
                            inserted_sent = False
                            break

                        has_reconnected = True
                        last_card_ok = time.time()
                        break

                    if apdu == "00000000000000":
                        logging.warning(f"Thread {thread_id}: Server requested CARD RESET (00000000000000 received).")
                        
                        # Acknowledge the dummy APDU to drop the server's stuck schedule
                        fetch_apdu_from_server(
                            sock, combined_identifier, thread_id,
                            response_data="", status="9000", pre_apdu=apdu,
                            vehicle_schedule_id=vehicle_schedule_id
                        )

                        vehicle_schedule_id = None
                        auth_start_time = 0
                        try:
                            connection.disconnect()
                        except Exception:
                            pass
                        
                        connection = connect_reader(reader_index)
                        if not connection:
                            company_name = None
                            combined_identifier = None
                            inserted_sent = False
                            break

                        # Re-run APDU_COMMANDS after cold reconnect to restore the
                        # card's EF selection context so subsequent auth APDUs do not
                        # return SW 6986 ("no current EF selected").
                        _ok, _parts = True, []
                        for _i, _cmd in enumerate(APDU_COMMANDS):
                            _d, _s = execute_apdu(connection, _cmd, thread_id)
                            if _d is None or _s != "9000":
                                logging.warning(f"Thread {thread_id}: Re-init {_cmd} failed ({_s}) after reset.")
                                _ok = False
                                break
                            if _i in [1, 3]:
                                _parts.append(_d)
                        if _ok and len(_parts) == 2:
                            combined_identifier = "".join(_parts)
                            logging.info(f"Thread {thread_id}: Card context restored after reset.")
                        else:
                            logging.error(f"Thread {thread_id}: Re-init failed after reset — treating as removal.")
                            try: connection.disconnect()
                            except Exception: pass
                            connection = None
                            company_name = None
                            combined_identifier = None
                            inserted_sent = False
                            break

                        has_reconnected = True
                        last_card_ok = time.time()
                        break

                    data, status = execute_apdu(connection, apdu, thread_id)
                    if status == "6A82":
                        logging.warning(f"Thread {thread_id}: APDU {apdu} returned 6A82. Resetting context to MF (00A4000000) and retrying.")
                        execute_apdu(connection, "00A4000000", thread_id) # Select Root MF
                        data, status = execute_apdu(connection, apdu, thread_id) # Retry original APDU
                    elif status == "6986":
                        # SW 6986 = "no current EF selected" — card lost its context,
                        # typically after a reconnect without re-running APDU_COMMANDS.
                        # Re-run APDU_COMMANDS to restore EF selection, then retry.
                        logging.warning(f"Thread {thread_id}: APDU {apdu} returned 6986 — restoring EF context.")
                        _ctx_ok = True
                        for _cmd in APDU_COMMANDS:
                            _, _s = execute_apdu(connection, _cmd, thread_id)
                            if _s != "9000":
                                _ctx_ok = False
                                break
                        if _ctx_ok:
                            data, status = execute_apdu(connection, apdu, thread_id)
                            logging.info(f"Thread {thread_id}: Retry after 6986 -> {status}")
                    
                    if data is None or status is None:
                        logging.error(f"Thread {thread_id}: Auth APDU {apdu} failed (hardware/connection drop)")
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
                        break

                    data_apdu = fetch_apdu_from_server(
                        sock, combined_identifier, thread_id,
                        response_data=data, status=status, pre_apdu=apdu,
                        vehicle_schedule_id=vehicle_schedule_id
                    )
                    if not data_apdu:
                        break
                    
                    apdu = data_apdu.get('apdu')
                    apdu_auth_status = int(data_apdu.get('auth_status', 1))
                    vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)

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
                logging.info(f"Thread {thread_id}: Auth status {auth_status}>1 -> reconnect reader")
                
                vehicle_schedule_id = None
                auth_start_time = 0

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
    global threads, initial_reader_count
    
    time.sleep(10) 
    
    while is_running:
        try:
            try:
                current_readers = get_readers_mapped() 
                current_count = len(current_readers)
                if current_count != initial_reader_count:
                    logging.critical(f"CRITICAL: Reader count mismatch. Expected {initial_reader_count}, found {current_count}. Exiting.")
                    os._exit(1) 
            except Exception as e:
                logging.critical(f"Supervisor: Failed to list PCSC readers: {e}. Exiting.")
                os._exit(1)

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
    global is_running, threads, READER_INDEX_MAPPING, supervisor_thread, initial_reader_count
    global SORTED_READERS_CACHE
    
    with data_lock:
        if is_running:
            return True
        is_running = True

    try:
        system_readers = get_readers_mapped() 
        SORTED_READERS_CACHE = system_readers
        
        count = len(system_readers)
        initial_reader_count = count
        
        if count == 0:
            logging.error("No smart-card readers detected.")
            with data_lock:
                is_running = False
            return False

        with data_lock:
            READER_INDEX_MAPPING = {i: i for i in range(count)}
            _init_reader_data(count)
            threads = []

        for i in range(count):
            t = threading.Thread(target=process_card, args=(i,), daemon=True)
            with data_lock:
                threads.append(t)
            t.start()

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
    with data_lock:
        if not is_running:
            return
        is_running = False
        local_threads = list(threads)

    for t in local_threads:
        try:
            if t:
                t.join(timeout=5)
        except Exception:
            pass

    with data_lock:
        threads = []
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
        logging.info("Logout attempt ignored — processing continues")
        return jsonify({"status": "success", "message": "Logout disabled — processing continues"})
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
    logging.info("Application starting... Auto-starting reader processing.")
    start_processing() 

    logging.info("Starting web server.")
    socketio.run(app, debug=False, host='0.0.0.0', port=WEB_PORT, allow_unsafe_werkzeug=True)