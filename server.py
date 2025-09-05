from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO
import socket
import json
import io
import csv
import time
import threading
import logging
from smartcard.System import readers
from smartcard.util import toHexString, toBytes
from smartcard.CardConnectionObserver import ConsoleCardConnectionObserver
from collections import deque
import gc
import os

app = Flask(__name__, template_folder='templates')
socketio = SocketIO(app, cors_allowed_origins="*")

# Custom SocketIO Handler for emitting logs
class SocketIOHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            socketio.emit('log_message', {'data': msg}, namespace='/logs')
        except Exception as e:
            print(f"SocketIOHandler error: {e}")

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        SocketIOHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
SERVER_IP = "206.189.24.200"
SERVER_PORT = 20119
APP_ID = "r06"
APDU_COMMANDS = [
    "00A4020C020002",  # Select application 1
    "00B0000118",      # Read first identifier part
    "00A4020C020005",  # Select application 2
    "00B0000008"       # Read second identifier part
]
REQUEST_INTERVAL = 1
MAX_THREADS = 4  # Limited for Orange Pi Zero 2W
SOCKET_RETRY_INTERVAL = 5
SOCKET_RETRY_TIMEOUT = 60
READER_INDEX_MAPPING = {}

# Shared state
reader_data = {i: {
    "readerIndex": i,
    "status": "Disconnected",
    "companyName": "N/A",
    "atr": "N/A",
    "authentication": "Unknown",
    "presentTime": "N/A",
    "cardInsertTime": None
} for i in range(MAX_THREADS)}
is_running = False
threads = []
data_lock = threading.Lock()
history_data = deque(maxlen=1000)

def format_duration(seconds):
    if seconds is None:
        return "N/A"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def connect_reader(reader_index):
    try:
        reader_list = readers()
        if not reader_list:
            raise ValueError("No readers detected")
        mapped_index = READER_INDEX_MAPPING.get(reader_index, reader_index)
        if mapped_index >= len(reader_list):
            raise ValueError(f"No reader at index {mapped_index}")
        reader_name = reader_list[mapped_index].name
        logger.info(f"Thread {reader_index}: Connecting to {reader_name}")
        connection = reader_list[mapped_index].createConnection()
        observer = ConsoleCardConnectionObserver()
        connection.addObserver(observer)
        start_time = time.time()
        while time.time() - start_time < 5:
            try:
                connection.connect()
                break
            except Exception:
                time.sleep(0.1)
        else:
            logger.error(f"Thread {reader_index}: Connection timeout for {reader_name}")
            return None
        atr = toHexString(connection.getATR())
        logger.info(f"Thread {reader_index}: Connected with ATR {atr}")
        return connection
    except Exception as e:
        logger.error(f"Thread {reader_index}: Connection error: {e}")
        return None

def execute_apdu(connection, apdu, thread_id):
    try:
        data, sw1, sw2 = connection.transmit(toBytes(apdu))
        status = f"{sw1:02X}{sw2:02X}"
        logger.debug(f"Thread {thread_id}: APDU {apdu} -> {toHexString(data)}, {status}")
        return toHexString(data).replace(" ", ""), status
    except Exception as e:
        logger.error(f"Thread {thread_id}: APDU error for {apdu}: {e}")
        return None, None

def create_socket(thread_id):
    start_time = time.time()
    while time.time() - start_time < SOCKET_RETRY_TIMEOUT:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((SERVER_IP, SERVER_PORT))
            logger.info(f"Thread {thread_id}: Connected to {SERVER_IP}:{SERVER_PORT}")
            return sock
        except socket.error as e:
            logger.warning(f"Thread {thread_id}: Socket error: {e}, retrying...")
            time.sleep(SOCKET_RETRY_INTERVAL)
    logger.error(f"Thread {thread_id}: Failed to connect after {SOCKET_RETRY_TIMEOUT}s")
    return None

def send_receive(sock, payload, operation, thread_id):
    try:
        sock.sendall(payload)
        logger.debug(f"Thread {thread_id}: Sent {operation}: {payload.decode()}")
        response = sock.recv(4096).decode().strip()
        json_start = response.find('{')
        if json_start == -1:
            logger.error(f"Thread {thread_id}: No JSON in {operation} response: {response}")
            return None
        return json.loads(response[json_start:])
    except (socket.error, json.JSONDecodeError) as e:
        logger.error(f"Thread {thread_id}: {operation} error: {e}")
        return None

def send_identifier(sock, identifier, thread_id, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "app_id": APP_ID, "reader_no": thread_id + 1}
    if vehicle_schedule_id:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "atr", "data": payload_data}).encode()
    return send_receive(sock, payload, "send_identifier", thread_id)

def fetch_company_name(sock, identifier, thread_id, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "reader_no": thread_id + 1, "app_id": APP_ID}
    if vehicle_schedule_id:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "get_company_card", "data": payload_data}).encode()
    response = send_receive(sock, payload, "fetch_company_name", thread_id)
    if response and "data" in response and isinstance(response["data"], dict):
        company_name = response["data"].get(identifier.lower())
        if company_name:
            logger.info(f"Thread {thread_id}: Got company name: {company_name}")
            return company_name
        logger.warning(f"Thread {thread_id}: No company name for {identifier}")
    return None

def send_card_status(sock, identifier, thread_id, status, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "reader_no": thread_id + 1, "app_id": APP_ID}
    if vehicle_schedule_id:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": f"card_{status}", "data": payload_data}).encode()
    response = send_receive(sock, payload, f"send_card_status_{status}", thread_id)
    if response:
        logger.info(f"Thread {thread_id}: {status} status sent: {response}")
    else:
        logger.error(f"Thread {thread_id}: Failed to send {status} status")
    return response

def process_card(reader_index):
    thread_id = reader_index
    connection = None
    sock = None
    company_name = None
    vehicle_schedule_id = None
    combined_identifier = None

    try:
        while is_running:
            if not connection:
                connection = connect_reader(reader_index)
                if not connection:
                    with data_lock:
                        reader_data[reader_index].update({
                            "status": "Disconnected",
                            "presentTime": "N/A",
                            "cardInsertTime": None,
                            "atr": "N/A",
                            "companyName": "N/A",
                            "authentication": "Unknown"
                        })
                        history_data.append({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        })
                        if len(history_data) > 1000:
                            history_data.popleft()
                    time.sleep(REQUEST_INTERVAL)
                    continue

                identifier_parts = []
                for i, apdu in enumerate(APDU_COMMANDS):
                    data, status = execute_apdu(connection, apdu, thread_id)
                    if data is None or status != "9000":
                        logger.error(f"Thread {thread_id}: APDU {apdu} failed: {status}")
                        with data_lock:
                            reader_data[thread_id].update({
                                "status": "Disconnected",
                                "presentTime": "N/A",
                                "cardInsertTime": None,
                                "companyName": "N/A"
                            })
                            history_data.append({
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                                "readerIndex": reader_index,
                                "status": "Disconnected",
                                "companyName": "N/A",
                                "atr": "N/A",
                                "authentication": "Unknown",
                                "presentTime": "N/A"
                            })
                            if len(history_data) > 1000:
                                history_data.popleft()
                        connection.disconnect()
                        connection = None
                        break
                    if i in [1, 3]:
                        identifier_parts.append(data)

                if len(identifier_parts) != 2:
                    logger.error(f"Thread {thread_id}: Incomplete identifier parts")
                    with data_lock:
                        reader_data[thread_id].update({
                            "status": "Disconnected",
                            "presentTime": "N/A",
                            "cardInsertTime": None,
                            "companyName": "N/A"
                        })
                        history_data.append({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        })
                        if len(history_data) > 1000:
                            history_data.popleft()
                    connection.disconnect()
                    connection = None
                    continue

                combined_identifier = "".join(identifier_parts)
                with data_lock:
                    reader_data[thread_id].update({
                        "status": "Connected",
                        "atr": combined_identifier,
                        "presentTime": format_duration(0),
                        "cardInsertTime": time.time(),
                        "companyName": "N/A"
                    })
                    history_data.append({
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                        "readerIndex": reader_index,
                        "status": "Connected",
                        "companyName": "N/A",
                        "atr": combined_identifier,
                        "authentication": "Unknown",
                        "presentTime": format_duration(0)
                    })
                    if len(history_data) > 1000:
                        history_data.popleft()

                sock = create_socket(thread_id)
                if not sock:
                    with data_lock:
                        reader_data[thread_id].update({
                            "status": "Disconnected",
                            "presentTime": "N/A",
                            "cardInsertTime": None,
                            "companyName": "N/A"
                        })
                        history_data.append({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        })
                        if len(history_data) > 1000:
                            history_data.popleft()
                    connection.disconnect()
                    connection = None
                    continue

                send_card_status(sock, combined_identifier, thread_id, "inserted", vehicle_schedule_id)

            with data_lock:
                if reader_data[thread_id]["cardInsertTime"]:
                    reader_data[thread_id]["presentTime"] = format_duration(time.time() - reader_data[thread_id]["cardInsertTime"])
                    history_data.append({
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    })
                    if len(history_data) > 1000:
                        history_data.popleft()

            try:
                connection.getATR()
            except Exception as e:
                logger.error(f"Thread {thread_id}: ATR check failed: {e}")
                if sock and combined_identifier:
                    send_card_status(sock, combined_identifier, thread_id, "removed", vehicle_schedule_id)
                with data_lock:
                    reader_data[thread_id].update({
                        "status": "Card Removed",
                        "presentTime": "N/A",
                        "cardInsertTime": None,
                        "atr": "N/A",
                        "authentication": "Unknown",
                        "companyName": "N/A"
                    })
                    history_data.append({
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                        "readerIndex": reader_index,
                        "status": "Card Removed",
                        "companyName": "N/A",
                        "atr": "N/A",
                        "authentication": "Unknown",
                        "presentTime": "N/A"
                    })
                    if len(history_data) > 1000:
                        history_data.popleft()
                if connection:
                    connection.disconnect()
                    connection = None
                if sock:
                    sock.close()
                    sock = None
                company_name = None
                combined_identifier = None
                vehicle_schedule_id = None
                time.sleep(REQUEST_INTERVAL)
                continue

            if not company_name:
                company_name = fetch_company_name(sock, combined_identifier, thread_id, vehicle_schedule_id)
                with data_lock:
                    if company_name:
                        reader_data[thread_id]["companyName"] = company_name
                        history_data.append({
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                            "readerIndex": reader_index,
                            "status": reader_data[thread_id]["status"],
                            "companyName": company_name,
                            "atr": reader_data[thread_id]["atr"],
                            "authentication": reader_data[thread_id]["authentication"],
                            "presentTime": reader_data[thread_id]["presentTime"]
                        })
                        if len(history_data) > 1000:
                            history_data.popleft()

            time.sleep(REQUEST_INTERVAL)
            gc.collect()
    except Exception as e:
        logger.error(f"Thread {thread_id}: Unexpected error: {e}")
        with data_lock:
            reader_data[thread_id].update({
                "status": "Disconnected",
                "presentTime": "N/A",
                "cardInsertTime": None,
                "companyName": "N/A"
            })
            history_data.append({
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                "readerIndex": reader_index,
                "status": "Disconnected",
                "companyName": "N/A",
                "atr": "N/A",
                "authentication": "Unknown",
                "presentTime": "N/A"
            })
            if len(history_data) > 1000:
                history_data.popleft()
    finally:
        if sock:
            sock.close()
            logger.info(f"Thread {thread_id}: Socket closed")
        if connection:
            try:
                connection.disconnect()
                logger.info(f"Thread {thread_id}: Reader disconnected")
            except Exception as e:
                logger.error(f"Thread {thread_id}: Disconnect error: {e}")

def start_processing():
    global is_running, threads, READER_INDEX_MAPPING
    with data_lock:
        if is_running:
            return True
        is_running = True
        try:
            reader_list = readers()
            if not reader_list:
                logger.error("No readers detected")
                return False
            READER_INDEX_MAPPING = {i: i for i in range(min(MAX_THREADS, len(reader_list)))}
            threads = [threading.Thread(target=process_card, args=(i,)) for i in range(MAX_THREADS)]
            for t in threads:
                t.daemon = True
                t.start()
            logger.info(f"Started {len(threads)} threads at {time.strftime('%H:%M:%S')}")
            return True
        except Exception as e:
            logger.error(f"Start processing failed: {e}")
            is_running = False
            return False

def stop_processing():
    global is_running, threads
    with data_lock:
        if not is_running:
            return
        is_running = False
        for t in threads:
            t.join(timeout=5)
        threads.clear()
        for i in range(MAX_THREADS):
            reader_data[i].update({
                "status": "Disconnected",
                "presentTime": "N/A",
                "cardInsertTime": None,
                "companyName": "N/A",
                "atr": "N/A",
                "authentication": "Unknown"
            })
        history_data.clear()
        logger.info(f"Stopped processing at {time.strftime('%H:%M:%S')}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/readers')
def get_readers():
    with data_lock:
        data = [{k: v for k, v in reader.items() if k != "cardInsertTime"} for reader in reader_data.values()]
        return jsonify({"status": "success", "data": data})

@app.route('/history')
def get_history():
    reader_index = request.args.get('readerIndex', type=int)
    with data_lock:
        if reader_index is not None:
            filtered = [entry for entry in history_data if entry.get('readerIndex') == reader_index]
            return jsonify({"status": "success", "data": filtered})
        return jsonify({"status": "success", "data": list(history_data)})

@app.route('/download_history')
def download_history():
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

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or 'username' not in data or 'password' not in data:
        logger.error("Login failed: Invalid JSON")
        return jsonify({"status": "error", "message": "Invalid credentials"}), 400
    if data['username'] == 'techvezoto' and data['password'] == 'techvezoto@1122':
        if start_processing():
            logger.info("Login successful, processing started")
            return jsonify({"status": "success"})
        logger.error("Login failed: Processing start failed")
        return jsonify({"status": "error", "message": "Failed to start processing"}), 500
    logger.warning("Login failed: Invalid credentials")
    return jsonify({"status": "error", "message": "Invalid credentials"}), 401

@app.route('/logout', methods=['POST'])
def logout():
    stop_processing()
    logger.info("Logout successful, processing stopped")
    return jsonify({"status": "success"})

@app.route('/graphs.html')
def graphs():
    return render_template('graphs.html')

@app.route('/logs.html')
def logs():
    return render_template('logs.html')

@socketio.on('connect', namespace='/logs')
def handle_connect():
    logger.info("Client connected to /logs")

@socketio.on('disconnect', namespace='/logs')
def handle_disconnect():
    logger.info("Client disconnected from /logs")

if __name__ == "__main__":
    gc.set_threshold(700, 10, 10)
    try:
        logger.info(f"Starting server at {time.strftime('%H:%M:%S')}")
        socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
    except Exception as e:
        logger.error(f"Server failed: {e}")
    finally:
        stop_processing()
