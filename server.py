from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO
import socket
import json, io, csv
import time
import threading
import logging
from smartcard.System import readers
from smartcard.util import toHexString, toBytes
from smartcard.CardConnectionObserver import ConsoleCardConnectionObserver

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

class SocketIOHandler(logging.Handler):
    def emit(self, record):
        try:
            socketio.emit('log_message', {'data': self.format(record)}, namespace='/logs')
        except Exception as e:
            print(f"SocketIOHandler error: {e}")

root_logger = logging.getLogger('')
root_logger.setLevel(logging.DEBUG)
fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(fmt)

sh = SocketIOHandler()
sh.setLevel(logging.DEBUG)
sh.setFormatter(fmt)

root_logger.addHandler(ch)
root_logger.addHandler(sh)

SERVER_IP = "206.189.24.200"
SERVER_PORT = 20119
APP_ID = "r06"

APDU_COMMANDS = [
    "00A4020C020002",
    "00B0000118",
    "00A4020C020005",
    "00B0000008"
]

REQUEST_INTERVAL = 1.0
NUM_SLOTS = 16
SOCKET_CONNECT_TIMEOUT = 5
SOCKET_RETRY_INTERVAL = 5
SOCKET_RETRY_WINDOW = 60
ABSENT_READER_BACKOFF = 2.5
READER_ENUM_INTERVAL = 10
LOG_MISS_EVERY = 30

data_lock = threading.Lock()
is_running = False
threads = []
stop_event = threading.Event()

system_reader_names = []
system_reader_lock = threading.Lock()

def _enum_readers():
    """Safely enumerate PC/SC readers (names)."""
    try:
        return [r.name for r in readers()]
    except Exception as e:
        logging.error(f"PC/SC reader enumeration failed: {e}")
        return []

def reader_count():
    with system_reader_lock:
        return len(system_reader_names)

def reader_name_by_index(idx):
    with system_reader_lock:
        if 0 <= idx < len(system_reader_names):
            return system_reader_names[idx]
        return None

def update_system_readers_periodically():
    """Background task to refresh the list of readers every few seconds."""
    last = []
    while not stop_event.is_set():
        now = _enum_readers()
        with system_reader_lock:
            system_reader_names.clear()
            system_reader_names.extend(now)
        if now != last:
            logging.info(f"PC/SC readers: {now} (count={len(now)})")
            last = now
        stop_event.wait(READER_ENUM_INTERVAL)

def format_duration(seconds):
    if seconds is None:
        return "N/A"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

reader_data = {
    i: {
        "readerIndex": i,
        "status": "Disconnected",
        "companyName": "N/A",
        "atr": "N/A",
        "authentication": "Unknown",
        "presentTime": "N/A",
        "cardInsertTime": None
    } for i in range(NUM_SLOTS)
}

HISTORY_LIMIT = 2000
history_data = []

def add_history(entry):
    history_data.append(entry)
    if len(history_data) > HISTORY_LIMIT:
        history_data.pop(0)

def connect_reader_by_system_index(sys_index, slot_id):
    """Connect to PC/SC reader by current system index. Returns connection or None."""
    try:
        rlist = readers()
        if sys_index >= len(rlist):
            raise IndexError(f"no reader at system index {sys_index}")
        rdr = rlist[sys_index]
        conn = rdr.createConnection()
        conn.addObserver(ConsoleCardConnectionObserver())
        conn.connect()
        atr = toHexString(conn.getATR())
        logging.info(f"Slot {slot_id}: Connected to {rdr.name} (sys#{sys_index}) ATR={atr}")
        return conn
    except Exception as e:
        logging.debug(f"Slot {slot_id}: connect_reader_by_system_index failed: {e}")
        return None

def execute_apdu(connection, apdu, slot_id):
    try:
        data, sw1, sw2 = connection.transmit(toBytes(apdu))
        status = f"{sw1:02X}{sw2:02X}"
        logging.debug(f"Slot {slot_id}: APDU {apdu} -> {toHexString(data)} [{status}]")
        return toHexString(data).replace(" ", ""), status
    except Exception as e:
        logging.error(f"Slot {slot_id}: APDU {apdu} error: {e}")
        return None, None

def create_socket(slot_id):
    """Create a TCP socket with keepalive and retry for a short window."""
    start = time.time()
    while time.time() - start < SOCKET_RETRY_WINDOW and not stop_event.is_set():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(SOCKET_CONNECT_TIMEOUT)

            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                s.setsockopt(socket.IPPROTO_TCP, 0x10, 60)   # TCP_KEEPIDLE
                s.setsockopt(socket.IPPROTO_TCP, 0x11, 10)   # TCP_KEEPINTVL
                s.setsockopt(socket.IPPROTO_TCP, 0x12, 6)    # TCP_KEEPCNT
            except Exception:
                pass

            s.connect((SERVER_IP, SERVER_PORT))
            logging.info(f"Slot {slot_id}: Connected to server {SERVER_IP}:{SERVER_PORT}")
            return s
        except Exception as e:
            logging.warning(f"Slot {slot_id}: socket connect failed: {e}; retrying in {SOCKET_RETRY_INTERVAL}s")
            if stop_event.wait(SOCKET_RETRY_INTERVAL):
                break
    logging.error(f"Slot {slot_id}: socket connect failed for {SOCKET_RETRY_WINDOW}s window")
    return None

def send_receive(sock, payload, op, slot_id):
    try:
        sock.sendall(payload)
        logging.debug(f"Slot {slot_id}: TX {op}: {payload.decode(errors='ignore')}")
        buf = sock.recv(8192).decode(errors='ignore').strip()
        jpos = buf.find('{')
        if jpos == -1:
            logging.error(f"Slot {slot_id}: {op} response has no JSON: {buf!r}")
            return None
        try:
            return json.loads(buf[jpos:])
        except json.JSONDecodeError as e:
            logging.error(f"Slot {slot_id}: {op} JSON parse error: {e}; payload: {buf[jpos:jpos+256]}")
            return None
    except Exception as e:
        logging.error(f"Slot {slot_id}: socket error during {op}: {e}")
        return None

def send_identifier(sock, identifier, slot_id, vehicle_schedule_id=None):
    data = {"atr": identifier, "app_id": APP_ID, "reader_no": slot_id + 1}
    if vehicle_schedule_id is not None:
        data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "atr", "data": data}).encode()
    return send_receive(sock, payload, "send_identifier", slot_id)

def fetch_company_name(sock, identifier, slot_id, vehicle_schedule_id=None):
    data = {"atr": identifier, "reader_no": slot_id + 1, "app_id": APP_ID}
    if vehicle_schedule_id is not None:
        data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "get_company_card", "data": data}).encode()
    resp = send_receive(sock, payload, "fetch_company_name", slot_id)
    if resp and isinstance(resp.get("data"), dict):
        name = resp["data"].get(identifier.lower())
        if name:
            logging.info(f"Slot {slot_id}: Company: {name}")
            return name
        logging.warning(f"Slot {slot_id}: No company for ATR {identifier}; resp keys: {list(resp['data'])}")
    else:
        logging.error(f"Slot {slot_id}: invalid company response: {resp}")
    return None

def send_card_status(sock, identifier, slot_id, status, vehicle_schedule_id=None):
    data = {"atr": identifier, "reader_no": slot_id + 1, "app_id": APP_ID}
    if vehicle_schedule_id is not None:
        data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": f"card_{status}", "data": data}).encode()
    resp = send_receive(sock, payload, f"card_{status}", slot_id)
    if resp:
        logging.info(f"Slot {slot_id}: sent card_{status}")
    else:
        logging.error(f"Slot {slot_id}: failed card_{status}")
    return resp

def fetch_apdu_from_server(sock, identifier, slot_id, response_data=None, status=None, pre_apdu=None, vehicle_schedule_id=None):
    data = {"atr": identifier, "app_id": APP_ID, "reader_no": slot_id + 1}
    msg_type = "apdu"
    if status is not None and response_data is not None:
        msg_type = "response"
        data["response"] = response_data + status
        data["apdu"] = pre_apdu
    if vehicle_schedule_id is not None:
        data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": msg_type, "data": data}).encode()
    resp = send_receive(sock, payload, "fetch_apdu", slot_id)
    if resp and isinstance(resp.get("data"), dict):
        return resp["data"]
    logging.error(f"Slot {slot_id}: invalid APDU response: {resp}")
    return None

def process_slot(slot_id):
    """Worker loop for one UI slot. It will bind to reader sys-index == slot_id when available."""
    last_miss_log = 0.0
    connection = None
    sock = None
    vehicle_schedule_id = None
    combined_identifier = None
    prev_auth_status = -1
    has_reconnected = False

    while is_running and not stop_event.is_set():
        sys_name = reader_name_by_index(slot_id)
        if sys_name is None:
            now = time.time()
            if now - last_miss_log > LOG_MISS_EVERY:
                logging.debug(f"Slot {slot_id}: no reader present (waiting)")
                last_miss_log = now
            with data_lock:
                rd = reader_data[slot_id]
                rd["status"] = "Disconnected"
                rd["companyName"] = "N/A"
                rd["atr"] = "N/A"
                rd["authentication"] = "Unknown"
                rd["presentTime"] = "N/A"
                rd["cardInsertTime"] = None
                add_history({
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                    "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                    "atr": rd["atr"], "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                })
            if connection:
                try: connection.disconnect()
                except Exception: pass
                connection = None
            if sock:
                try: sock.close()
                except Exception: pass
                sock = None
            if stop_event.wait(ABSENT_READER_BACKOFF):
                break
            continue

        if connection is None:
            current_names = _enum_readers()
            try:
                sys_index = current_names.index(sys_name)
            except ValueError:
                if stop_event.wait(ABSENT_READER_BACKOFF):
                    break
                continue

            connection = connect_reader_by_system_index(sys_index, slot_id)
            if not connection:
                if stop_event.wait(ABSENT_READER_BACKOFF):
                    break
                continue

            parts = []
            failed = False
            for i, apdu in enumerate(APDU_COMMANDS):
                data, status = execute_apdu(connection, apdu, slot_id)
                if data is None or status != "9000":
                    failed = True
                    break
                if i in (1, 3):
                    parts.append(data)
            if failed or len(parts) != 2:
                logging.error(f"Slot {slot_id}: failed to read identifier; resetting reader")
                try: connection.disconnect()
                except Exception: pass
                connection = None
                if stop_event.wait(REQUEST_INTERVAL):
                    break
                continue

            combined_identifier = "".join(parts)
            with data_lock:
                rd = reader_data[slot_id]
                rd["status"] = "Connected"
                rd["atr"] = combined_identifier
                rd["companyName"] = "N/A"
                rd["authentication"] = "Unknown"
                rd["cardInsertTime"] = time.time()
                rd["presentTime"] = format_duration(0)
                add_history({
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                    "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                    "atr": rd["atr"], "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                })

            sock = create_socket(slot_id)
            if not sock:
                with data_lock:
                    rd = reader_data[slot_id]
                    rd["status"] = "Disconnected"
                    rd["presentTime"] = "N/A"
                    rd["cardInsertTime"] = None
                    rd["companyName"] = "N/A"
                    rd["authentication"] = "Unknown"
                    add_history({
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                        "atr": "N/A", "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                    })
                try: connection.disconnect()
                except Exception: pass
                connection = None
                if stop_event.wait(REQUEST_INTERVAL):
                    break
                continue

            send_card_status(sock, combined_identifier, slot_id, "inserted", vehicle_schedule_id)

        with data_lock:
            rd = reader_data[slot_id]
            if rd["cardInsertTime"]:
                rd["presentTime"] = format_duration(time.time() - rd["cardInsertTime"])
                add_history({
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                    "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                    "atr": rd["atr"], "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                })
            else:
                rd["presentTime"] = "N/A"

        try:
            connection.getATR()
        except Exception as e:
            logging.warning(f"Slot {slot_id}: card lost ({e}); cleaning up")
            if sock and combined_identifier:
                send_card_status(sock, combined_identifier, slot_id, "removed", vehicle_schedule_id)
            with data_lock:
                rd = reader_data[slot_id]
                rd["status"] = "Card Removed"
                rd["presentTime"] = "N/A"
                rd["cardInsertTime"] = None
                rd["atr"] = "N/A"
                rd["authentication"] = "Unknown"
                rd["companyName"] = "N/A"
                add_history({
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                    "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                    "atr": rd["atr"], "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                })
            try: connection.disconnect()
            except Exception: pass
            connection = None
            try: sock.close()
            except Exception: pass
            sock = None
            combined_identifier = None
            vehicle_schedule_id = None
            if stop_event.wait(REQUEST_INTERVAL):
                break
            continue

        resp = send_identifier(sock, combined_identifier, slot_id, vehicle_schedule_id)
        if not resp:
            try: sock.close()
            except Exception: pass
            sock = create_socket(slot_id)
            if stop_event.wait(REQUEST_INTERVAL):
                break
            continue

        if isinstance(resp.get("data"), dict):
            vehicle_schedule_id = resp["data"].get("vehicle_schedule_id") or vehicle_schedule_id

        auth_status = resp.get("data", {}).get(combined_identifier.lower(), -1)
        with data_lock:
            rd = reader_data[slot_id]
            rd["authentication"] = (
                "No Authentication Required" if auth_status == 0 else
                "Authentication Required" if auth_status == 1 else
                f"Authentication Failed ({auth_status})" if auth_status > 1 else
                "Unknown"
            )
            add_history({
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                "atr": rd["atr"], "authentication": rd["authentication"], "presentTime": rd["presentTime"]
            })

        if auth_status == 1:
            logging.info(f"Slot {slot_id}: authentication required")
            with data_lock:
                reader_data[slot_id]["status"] = "Connected"
            dapdu = fetch_apdu_from_server(sock, combined_identifier, slot_id, vehicle_schedule_id=vehicle_schedule_id)
            if not dapdu:
                if stop_event.wait(REQUEST_INTERVAL): break
                continue
            apdu = dapdu.get("apdu")
            vehicle_schedule_id = dapdu.get("vehicle_schedule_id", vehicle_schedule_id)

            while apdu and apdu != "00000000000000":
                if apdu == "11111111111111":
                    dapdu = fetch_apdu_from_server(sock, combined_identifier, slot_id, vehicle_schedule_id=vehicle_schedule_id)
                    if not dapdu: break
                    apdu = dapdu.get("apdu")
                    vehicle_schedule_id = dapdu.get("vehicle_schedule_id", vehicle_schedule_id)
                    continue

                data, status = execute_apdu(connection, apdu, slot_id)
                if data is None or status is None:
                    logging.error(f"Slot {slot_id}: auth APDU failed; resetting reader")
                    with data_lock:
                        rd = reader_data[slot_id]
                        rd["status"] = "Disconnected"
                        rd["presentTime"] = "N/A"
                        rd["cardInsertTime"] = None
                        rd["companyName"] = "N/A"
                        add_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                            "atr": "N/A", "authentication": "Unknown", "presentTime": rd["presentTime"]
                        })
                    try: connection.disconnect()
                    except Exception: pass
                    connection = None
                    combined_identifier = None
                    break

                dapdu = fetch_apdu_from_server(sock, combined_identifier, slot_id,
                                               response_data=data, status=status, pre_apdu=apdu,
                                               vehicle_schedule_id=vehicle_schedule_id)
                if not dapdu: break
                apdu = dapdu.get("apdu")
                vehicle_schedule_id = dapdu.get("vehicle_schedule_id", vehicle_schedule_id)

            if apdu == "00000000000000":
                data, status = execute_apdu(connection, apdu, slot_id)
                if data is not None and status is not None:
                    logging.info(f"Slot {slot_id}: authentication completed")
                    with data_lock:
                        rd = reader_data[slot_id]
                        rd["status"] = "Connected"
                        rd["authentication"] = "No Authentication Required"
                        rd["cardInsertTime"] = time.time()
                        rd["presentTime"] = format_duration(0)
                        rd["companyName"] = "N/A"
                        add_history({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                            "atr": rd["atr"], "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                        })
                    # Reconnect the PC/SC link once after auth finish
                    try: connection.disconnect()
                    except Exception: pass
                    connection = None
                    has_reconnected = True

        elif auth_status == 0:
            logging.debug(f"Slot {slot_id}: no authentication required")
            with data_lock:
                rd = reader_data[slot_id]
                rd["status"] = "Card-Connected"
                if not rd["cardInsertTime"]:
                    rd["cardInsertTime"] = time.time()
                    rd["presentTime"] = format_duration(0)
                add_history({
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                    "readerIndex": slot_id, "status": rd["status"], "companyName": rd["companyName"],
                    "atr": rd["atr"], "authentication": rd["authentication"], "presentTime": rd["presentTime"]
                })
            name = fetch_company_name(sock, combined_identifier, slot_id, vehicle_schedule_id)
            with data_lock:
                reader_data[slot_id]["companyName"] = name if name else "N/A"
            has_reconnected = False

        elif auth_status > 1 and not has_reconnected and prev_auth_status <= 1:
            logging.warning(f"Slot {slot_id}: auth status {auth_status} -> reconnect reader")
            try: connection.disconnect()
            except Exception: pass
            connection = None
            has_reconnected = True

        else:
            has_reconnected = False

        prev_auth_status = auth_status
        if stop_event.wait(REQUEST_INTERVAL):
            break

    if sock:
        try: sock.close()
        except Exception: pass
    if connection:
        try: connection.disconnect()
        except Exception: pass
    logging.info(f"Slot {slot_id}: worker stopped")

def start_processing():
    global is_running, threads
    with data_lock:
        if is_running:
            return True
        is_running = True

    stop_event.clear()

    enum_thread = threading.Thread(target=update_system_readers_periodically, daemon=True)
    enum_thread.start()
    threads = [enum_thread]
    names = _enum_readers()
    with system_reader_lock:
        system_reader_names.clear()
        system_reader_names.extend(names)
    logging.info(f"Initial PC/SC readers: {names}")

    for i in range(NUM_SLOTS):
        t = threading.Thread(target=process_slot, args=(i,), daemon=True)
        t.start()
        threads.append(t)

    logging.info(f"Started processing for {NUM_SLOTS} UI slots at {time.strftime('%H:%M:%S', time.localtime())}")
    return True

def stop_processing():
    global is_running, threads
    is_running = False
    stop_event.set()
    for t in threads:
        try:
            t.join(timeout=2)
        except Exception:
            pass
    threads = []
    with data_lock:
        for i in range(NUM_SLOTS):
            reader_data[i] = {
                "readerIndex": i,
                "status": "Disconnected",
                "companyName": "N/A",
                "atr": "N/A",
                "authentication": "Unknown",
                "presentTime": "N/A",
                "cardInsertTime": None
            }
    logging.info(f"Stopped processing at {time.strftime('%H:%M:%S', time.localtime())}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/readers')
def get_readers():
    try:
        with data_lock:
            data = [{k: v for k, v in rd.items() if k != "cardInsertTime"} for rd in reader_data.values()]
            logging.debug(f"Serving reader data at {time.strftime('%H:%M:%S', time.localtime())}: {data}")
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        logging.error(f"/readers error: {e}")
        return jsonify({"status": "error", "message": str(e), "data": []}), 500

@app.route('/history')
def get_history():
    try:
        idx = request.args.get('readerIndex', type=int)
        with data_lock:
            if idx is not None:
                filtered = [e for e in history_data if e.get("readerIndex") == idx]
                return jsonify({"status": "success", "data": filtered})
            return jsonify({"status": "success", "data": history_data})
    except Exception as e:
        logging.error(f"/history error: {e}")
        return jsonify({"status": "error", "message": str(e), "data": []}), 500

@app.route('/download_history')
def download_history():
    try:
        with data_lock:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=["timestamp","readerIndex","status","companyName","atr","authentication","presentTime"])
            writer.writeheader()
            for entry in history_data:
                writer.writerow(entry)
            output.seek(0)
            return send_file(io.BytesIO(output.getvalue().encode('utf-8')),
                             mimetype='text/csv',
                             as_attachment=True,
                             download_name='history.csv')
    except Exception as e:
        logging.error(f"/download_history error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        logging.debug(f"Login headers: {dict(request.headers)} body: {request.get_data()}")
        data = request.get_json()
        if data is None:
            return jsonify({"status": "error", "message": "Request must be application/json"}), 415
        if data.get('username') == 'techvezoto' and data.get('password') == 'techvezoto@1122':
            ok = start_processing()
            if ok:
                return jsonify({"status": "success"})
            return jsonify({"status": "error", "message": "Failed to start reader processing"}), 500
        return jsonify({"status": "error", "message": "Invalid username or password"}), 401
    except Exception as e:
        logging.error(f"/login error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/logout', methods=['POST'])
def logout():
    try:
        stop_processing()
        return jsonify({"status": "success"})
    except Exception as e:
        logging.error(f"/logout error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/graphs.html')
def graphs():
    return render_template('graphs.html')

@app.route('/logs.html')
def logs_page():
    return render_template('logs.html')

@socketio.on('connect', namespace='/logs')
def handle_connect():
    logging.info(f"Client connected to /logs at {time.strftime('%H:%M:%S', time.localtime())}")

@socketio.on('disconnect', namespace='/logs')
def handle_disconnect():
    logging.info(f"Client disconnected from /logs at {time.strftime('%H:%M:%S', time.localtime())}")

if __name__ == "__main__":
    socketio.run(app, debug=False, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)

