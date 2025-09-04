from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO
import socket
import json, io, csv
import time
import threading
import logging
from logging.handlers import RotatingFileHandler
from smartcard.System import readers
from smartcard.util import toHexString, toBytes
from smartcard.CardConnectionObserver import ConsoleCardConnectionObserver
from collections import deque
import gc

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Custom SocketIO Handler for emitting logs
class SocketIOHandler(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:  # Only emit WARNING and ERROR
            try:
                msg = self.format(record)
                socketio.emit('log_message', {'data': msg}, namespace='/logs')
            except Exception as e:
                print(f"SocketIOHandler error: {e}")

# Configure root logger
logging.getLogger('').setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# File handler for disk logging (rotates at 1MB, keeps 5 backups)
file_handler = RotatingFileHandler('server.log', maxBytes=1_000_000, backupCount=5)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

# SocketIO handler for streaming to logs.html
socketio_handler = SocketIOHandler()
socketio_handler.setLevel(logging.WARNING)  # Restrict to WARNING/ERROR
socketio_handler.setFormatter(formatter)

# Add handlers to root logger (no console handler to reduce I/O)
logging.getLogger('').addHandler(file_handler)
logging.getLogger('').addHandler(socketio_handler)

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
REQUEST_INTERVAL = 5
NUM_CARDS = 16
SOCKET_RETRY_INTERVAL = 5
SOCKET_RETRY_TIMEOUT = 60000
READER_INDEX_MAPPING = {}
HISTORY_LIMIT = 100  # Reduced to 100 to save memory

# Shared reader data
reader_data = {i: {
    "readerIndex": i,
    "status": "Disconnected",
    "companyName": "N/A",
    "atr": "N/A",
    "authentication": "Unknown",
    "presentTime": "N/A",
    "cardInsertTime": None
} for i in range(NUM_CARDS)}
is_running = False
threads = []
data_lock = threading.Lock()
history_data = deque(maxlen=HISTORY_LIMIT)

def format_duration(seconds):
    if seconds is None:
        return "N/A"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def connect_reader(reader_index):
    """Connect to a specific reader by index with retry logic."""
    max_attempts = 3
    timeout_seconds = 15  # Increased to 15 seconds for stability

    for attempt in range(max_attempts):
        try:
            reader_list = readers()
            if not reader_list:
                raise ValueError("No readers detected")
            mapped_index = READER_INDEX_MAPPING.get(reader_index, reader_index)
            if mapped_index >= len(reader_list):
                raise ValueError(f"No reader available for mapped index {mapped_index}")
            reader_name = reader_list[mapped_index].name
            logging.info(f"Thread {reader_index}: Attempt {attempt + 1}/{max_attempts} - Attempting to connect to {reader_name}")
            
            connection = reader_list[mapped_index].createConnection()
            observer = ConsoleCardConnectionObserver()
            connection.addObserver(observer)
            start_time = time.time()
            while time.time() - start_time < timeout_seconds:
                try:
                    connection.connect()
                    logging.info(f"Thread {reader_index}: Successfully connected to {reader_name} on attempt {attempt + 1}")
                    break
                except Exception as e:
                    logging.debug(f"Thread {reader_index}: Connection attempt failed: {e}")
                    time.sleep(0.2)  # Increased sleep for stability
            else:
                logging.error(f"Thread {reader_index}: Timeout connecting to {reader_name} after {timeout_seconds}s (Attempt {attempt + 1}/{max_attempts})")
                continue

            try:
                atr = toHexString(connection.getATR())
                logging.info(f"Thread {reader_index}: Connected to {reader_name} with ATR: {atr}")
                return connection
            except Exception as e:
                logging.error(f"Thread {reader_index}: Failed to read ATR from {reader_name}: {e}")
                connection.disconnect()
                continue

        except Exception as e:
            logging.error(f"Thread {reader_index}: Reader connection error on attempt {attempt + 1}: {e}")
            time.sleep(1)
    logging.error(f"Thread {reader_index}: Failed to connect after {max_attempts} attempts")
    return None

def execute_apdu(connection, apdu, thread_id):
    try:
        data, sw1, sw2 = connection.transmit(toBytes(apdu))
        status = f"{sw1:02X}{sw2:02X}"
        return toHexString(data).replace(" ", ""), status
    except Exception as e:
        logging.error(f"Thread {thread_id}: APDU execution error for {apdu}: {e}")
        return None, None

def create_socket(thread_id):
    start_time = time.time()
    while time.time() - start_time < SOCKET_RETRY_TIMEOUT:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((SERVER_IP, SERVER_PORT))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
            logging.info(f"Thread {thread_id}: Connected to server {SERVER_IP}:{SERVER_PORT} with keep-alive")
            return sock
        except socket.error as e:
            logging.error(f"Thread {thread_id}: Socket connection error: {e}. Retrying...")
            time.sleep(SOCKET_RETRY_INTERVAL)
    logging.error(f"Thread {thread_id}: Failed to connect after {SOCKET_RETRY_TIMEOUT}s")
    return None

def send_receive(sock, payload, operation, thread_id):
    retries = 0
    max_retries = 3
    while retries < max_retries:
        try:
            sock.sendall(payload)
            response = sock.recv(4096).decode().strip()
            json_start = response.find('{')
            if json_start == -1:
                logging.error(f"Thread {thread_id}: No JSON in {operation} response: {response}")
                return None
            json_data = response[json_start:]
            return json.loads(json_data)
        except socket.error as e:
            logging.error(f"Thread {thread_id}: Socket error during {operation} (retry {retries+1}/{max_retries}): {e}")
            retries += 1
            time.sleep(1)
            if sock:
                sock.close()
            sock = create_socket(thread_id)
            if not sock:
                return None
    logging.error(f"Thread {thread_id}: Max retries exceeded for {operation}")
    return None

def send_identifier(sock, identifier, thread_id, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "app_id": APP_ID, "reader_no": thread_id + 1}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "atr", "data": payload_data}).encode()
    return send_receive(sock, payload, "send_identifier", thread_id)

def fetch_company_name(sock, identifier, thread_id, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "reader_no": thread_id + 1, "app_id": APP_ID}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "get_company_card", "data": payload_data}).encode()
    response_data = send_receive(sock, payload, "fetch_company_name", thread_id)
    if response_data and isinstance(response_data.get("data"), dict):
        company_name = response_data["data"].get(identifier.lower())
        if company_name:
            logging.info(f"Thread {thread_id}: Received company name: {company_name}")
            return company_name
    return None

def send_card_status(sock, identifier, thread_id, status, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "reader_no": thread_id + 1, "app_id": APP_ID}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": f"card_{status}", "data": payload_data}).encode()
    response_data = send_receive(sock, payload, f"send_card_status_{status}", thread_id)
    return response_data

def fetch_apdu_from_server(sock, identifier, thread_id, response_data=None, status=None, pre_apdu=None, vehicle_schedule_id=None):
    payload_data = {"atr": identifier, "app_id": APP_ID, "reader_no": thread_id + 1}
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
            return data_apdu
    return None


def start_processing():
    """Start processing threads for all readers."""
    global is_running, threads
    if is_running:
        logging.warning("Processing already running, skipping start")
        return False
    is_running = True
    with data_lock:
        threads = []
        for i in range(NUM_CARDS):
            thread = threading.Thread(target=process_card, args=(i,), daemon=True)
            threads.append(thread)
            thread.start()
            logging.info(f"Started processing thread for reader index {i}")
    return True
def process_card(reader_index):
    """Process a single card in a separate thread."""
    thread_id = reader_index
    connection = None
    sock = None
    company_name = None
    vehicle_schedule_id = None
    has_reconnected = False
    prev_auth_status = -1
    combined_identifier = None
    last_auth_status = None
    company_fetched = False
    last_activity = time.time()
    last_present_time_update = 0
    last_reader_check = time.time()  # For dynamic reader handling
    last_reconnect_check = time.time()  # For keep-alive

    logging.info(f"Thread {thread_id}: Starting reader processing")
    while is_running:
        try:
            # Dynamic Reader Handling: Re-enumerate readers every 5 minutes
            if time.time() - last_reader_check >= 300:
                reader_list = readers()
                if not reader_list and connection:
                    logging.warning(f"Thread {thread_id}: No readers detected, disconnecting")
                    if connection:
                        connection.disconnect()
                        connection = None
                last_reader_check = time.time()

            if not connection:
                connection = connect_reader(reader_index)
                if not connection:
                    with data_lock:
                        history_entry = {
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        }
                        if not history_data or history_entry != history_data[-1]:
                            history_data.append(history_entry)
                        reader_data[reader_index].update({
                            "status": "Disconnected",
                            "presentTime": "N/A",
                            "cardInsertTime": None,
                            "atr": "N/A",
                            "companyName": "N/A",
                            "authentication": "Unknown"
                        })
                    company_name = None
                    time.sleep(5)  # Increased delay to 5 seconds after failed connection
                    continue

                # Attempt ATR reading and handle failure
                try:
                    atr = toHexString(connection.getATR())
                    logging.info(f"Thread {thread_id}: Read ATR: {atr}")
                    identifier_parts = []
                    for i, apdu in enumerate(APDU_COMMANDS):
                        data, status = execute_apdu(connection, apdu, thread_id)
                        if data is None or status != "9000":
                            logging.error(f"Thread {thread_id}: APDU {apdu} failed with status: {status}")
                            break
                        if i in [1, 3]:
                            identifier_parts.append(data)
                    if len(identifier_parts) == 2:
                        combined_identifier = "".join(identifier_parts)
                        logging.info(f"Thread {thread_id}: Combined identifier: {combined_identifier}")
                    else:
                        logging.error(f"Thread {thread_id}: Failed to collect identifier parts")
                        connection.disconnect()
                        connection = None
                        continue
                except Exception as e:
                    logging.error(f"Thread {thread_id}: Error reading ATR or APDUs: {e}")
                    connection.disconnect()
                    connection = None
                    continue

                with data_lock:
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": "Connected",
                        "companyName": "N/A",
                        "atr": combined_identifier,
                        "authentication": "Unknown",
                        "presentTime": format_duration(0)
                    }
                    if not history_data or history_entry != history_data[-1]:
                        history_data.append(history_entry)
                    reader_data[thread_id].update({
                        "status": "Connected",
                        "atr": combined_identifier,
                        "presentTime": format_duration(0),
                        "cardInsertTime": time.time(),
                        "companyName": "N/A",
                        "authentication": "Unknown"
                    })

                sock = create_socket(thread_id)
                if not sock:
                    connection.disconnect()
                    connection = None
                    with data_lock:
                        reader_data[thread_id].update({
                            "status": "Disconnected",
                            "presentTime": "N/A",
                            "cardInsertTime": None,
                            "atr": "N/A",
                            "companyName": "N/A",
                            "authentication": "Unknown"
                        })
                    time.sleep(REQUEST_INTERVAL)
                    continue

                send_card_status(sock, combined_identifier, thread_id, "inserted", vehicle_schedule_id)

            # Keep-Alive & Reconnect: Check connection every 30 minutes
            if time.time() - last_reconnect_check >= 1800:
                try:
                    connection.reconnect()
                    logging.info(f"Thread {thread_id}: Successfully reconnected to reader")
                except Exception as e:
                    logging.warning(f"Thread {thread_id}: Reconnect failed: {e}")
                    if connection:
                        connection.disconnect()
                        connection = None
                last_reconnect_check = time.time()

            # Update present time every 1 second for smoother display
            with data_lock:
                if reader_data[thread_id]["cardInsertTime"]:
                    current_time = time.time()
                    if current_time - last_present_time_update >= 1:  # Update every 1 second
                        reader_data[thread_id]["presentTime"] = format_duration(current_time - reader_data[thread_id]["cardInsertTime"])
                        logging.debug(f"Thread {thread_id}: Updated presentTime to {reader_data[thread_id]['presentTime']} for reader {reader_index}")
                        history_entry = {
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index,
                            "status": reader_data[thread_id]["status"],
                            "companyName": reader_data[thread_id]["companyName"],
                            "atr": reader_data[thread_id]["atr"],
                            "authentication": reader_data[thread_id]["authentication"],
                            "presentTime": reader_data[thread_id]["presentTime"]
                        }
                        if not history_data or history_entry != history_data[-1]:
                            history_data.append(history_entry)
                        last_present_time_update = current_time
                else:
                    reader_data[thread_id]["presentTime"] = "N/A"

            # Check connection status
            if connection and time.time() - last_activity > 3600:
                logging.info(f"Thread {thread_id}: Idle timeout; reconnecting.")
                try:
                    connection.disconnect()
                except:
                    pass
                connection = connect_reader(reader_index)
                last_activity = time.time()

            try:
                connection.getATR()
            except Exception as e:
                logging.error(f"Thread {thread_id}: Error during getATR: {e}")
                if sock and combined_identifier:
                    send_card_status(sock, combined_identifier, thread_id, "removed", vehicle_schedule_id)
                with data_lock:
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": "Card Removed",
                        "companyName": "N/A",
                        "atr": "N/A",
                        "authentication": "Unknown",
                        "presentTime": "N/A"
                    }
                    if not history_data or history_entry != history_data[-1]:
                        history_data.append(history_entry)
                    reader_data[thread_id].update({
                        "status": "Card Removed",
                        "presentTime": "N/A",
                        "cardInsertTime": None,
                        "atr": "N/A",
                        "authentication": "Unknown",
                        "companyName": "N/A"
                    })
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

            # Fetch authentication status
            response_data = send_identifier(sock, combined_identifier, thread_id, vehicle_schedule_id)
            if not response_data:
                logging.error(f"Thread {thread_id}: Failed to retrieve server response, attempting to reconnect")
                if sock:
                    sock.close()
                sock = create_socket(thread_id)
                if not sock:
                    connection.disconnect()
                    connection = None
                    with data_lock:
                        reader_data[thread_id].update({
                            "status": "Disconnected",
                            "presentTime": "N/A",
                            "cardInsertTime": None,
                            "atr": "N/A",
                            "companyName": "N/A",
                            "authentication": "Unknown"
                        })
                    time.sleep(REQUEST_INTERVAL)
                    continue
                time.sleep(REQUEST_INTERVAL)
                continue

            if isinstance(response_data.get("data"), dict):
                vehicle_schedule_id = response_data["data"].get("vehicle_schedule_id")

            auth_status = response_data.get("data", {}).get(combined_identifier.lower(), -1)
            with data_lock:
                reader_data[thread_id]["authentication"] = (
                    "No Authentication Required" if auth_status == 0 else
                    "Authentication Required" if auth_status == 1 else
                    f"Authentication Failed ({auth_status})" if auth_status > 1 else
                    "Unknown"
                )
                history_entry = {
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                    "readerIndex": reader_index,
                    "status": reader_data[thread_id]["status"],
                    "companyName": reader_data[thread_id]["companyName"],
                    "atr": reader_data[thread_id]["atr"],
                    "authentication": reader_data[thread_id]["authentication"],
                    "presentTime": reader_data[thread_id]["presentTime"]
                }
                if not history_data or history_entry != history_data[-1]:
                    history_data.append(history_entry)

            if auth_status == 1:
                logging.info(f"Thread {thread_id}: Authentication required")
                with data_lock:
                    reader_data[thread_id]["status"] = "Connected"
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    if not history_data or history_entry != history_data[-1]:
                        history_data.append(history_entry)
                data_apdu = fetch_apdu_from_server(sock, combined_identifier, thread_id, vehicle_schedule_id=vehicle_schedule_id)
                if not data_apdu:
                    logging.error(f"Thread {thread_id}: Failed to fetch APDU")
                    time.sleep(REQUEST_INTERVAL)
                    continue
                apdu = data_apdu.get('apdu')
                vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)

                iter_count = 0
                max_iters = 20
                while apdu and apdu != "00000000000000" and iter_count < max_iters:
                    iter_count += 1
                    if apdu == "11111111111111":
                        data_apdu = fetch_apdu_from_server(sock, combined_identifier, thread_id, vehicle_schedule_id=vehicle_schedule_id)
                        if not data_apdu:
                            break
                        apdu = data_apdu.get('apdu')
                        vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)
                        continue

                    data, status = execute_apdu(connection, apdu, thread_id)
                    if data is None or status is None:
                        logging.error(f"Thread {thread_id}: Authentication APDU {apdu} failed with status: {status}")
                        with data_lock:
                            history_entry = {
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index,
                                "status": "Disconnected",
                                "companyName": "N/A",
                                "atr": "N/A",
                                "authentication": "Unknown",
                                "presentTime": "N/A"
                            }
                            if not history_data or history_entry != history_data[-1]:
                                history_data.append(history_entry)
                            reader_data[thread_id].update({
                                "status": "Disconnected",
                                "presentTime": "N/A",
                                "cardInsertTime": None,
                                "companyName": "N/A"
                            })
                        connection.disconnect()
                        connection = None
                        company_name = None
                        combined_identifier = None
                        break
                    data_apdu = fetch_apdu_from_server(sock, combined_identifier, thread_id, response_data=data, status=status, pre_apdu=apdu, vehicle_schedule_id=vehicle_schedule_id)
                    if not data_apdu:
                        break
                    apdu = data_apdu.get('apdu')
                    vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)

                if apdu == "00000000000000":
                    data, status = execute_apdu(connection, apdu, thread_id)
                    if data is None or status is None:
                        logging.error(f"Thread {thread_id}: Final APDU {apdu} failed with status: {status}")
                    else:
                        logging.info(f"Thread {thread_id}: Authentication successfully completed")
                        with data_lock:
                            history_entry = {
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index,
                                "status": "Connected",
                                "companyName": "N/A",
                                "atr": reader_data[thread_id]["atr"],
                                "authentication": "No Authentication Required",
                                "presentTime": format_duration(0)
                            }
                            if not history_data or history_entry != history_data[-1]:
                                history_data.append(history_entry)
                            reader_data[thread_id].update({
                                "status": "Connected",
                                "authentication": "No Authentication Required",
                                "cardInsertTime": time.time(),
                                "presentTime": format_duration(0),
                                "companyName": "N/A"
                            })
                        connection.disconnect()
                        connection = None
                        connection = connect_reader(reader_index)
                        if not connection:
                            with data_lock:
                                reader_data[thread_id].update({
                                    "status": "Disconnected",
                                    "presentTime": "N/A",
                                    "cardInsertTime": None,
                                    "atr": "N/A",
                                    "companyName": "N/A",
                                    "authentication": "Unknown"
                                })
                            company_name = None
                            combined_identifier = None
                            break
                        has_reconnected = True
            elif auth_status == 0:
                logging.info(f"Thread {thread_id}: No authentication required")
                with data_lock:
                    reader_data[thread_id]["status"] = "Card-Connected"
                    if not reader_data[thread_id]["cardInsertTime"]:
                        reader_data[thread_id]["cardInsertTime"] = time.time()
                        reader_data[thread_id]["presentTime"] = format_duration(0)
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    if not history_data or history_entry != history_data[-1]:
                        history_data.append(history_entry)
                company_name = fetch_company_name(sock, combined_identifier, thread_id, vehicle_schedule_id)
                with data_lock:
                    reader_data[thread_id]["companyName"] = company_name if company_name else "N/A"
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    if not history_data or history_entry != history_data[-1]:
                        history_data.append(history_entry)
                has_reconnected = False
            elif auth_status > 1 and not has_reconnected and prev_auth_status <= 1:
                logging.info(f"Thread {thread_id}: Authentication status changed to {auth_status} > 1, disconnecting and reconnecting")
                with data_lock:
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": "Disconnected",
                        "companyName": "N/A",
                        "atr": "N/A",
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": "N/A"
                    }
                    if not history_data or history_entry != history_data[-1]:
                        history_data.append(history_entry)
                    reader_data[thread_id].update({
                        "status": "Disconnected",
                        "presentTime": "N/A",
                        "cardInsertTime": None,
                        "companyName": "N/A"
                    })
                connection.disconnect()
                connection = connect_reader(reader_index)
                if not connection:
                    with data_lock:
                        reader_data[thread_id].update({
                            "status": "Disconnected",
                            "presentTime": "N/A",
                            "cardInsertTime": None,
                            "atr": "N/A",
                            "companyName": "N/A",
                            "authentication": "Unknown"
                        })
                    company_name = None
                    combined_identifier = None
                    time.sleep(REQUEST_INTERVAL)
                    continue
                has_reconnected = True
            elif auth_status > 1 and has_reconnected:
                pass
            elif auth_status > 1 and prev_auth_status > 1:
                pass
            else:
                with data_lock:
                    reader_data[thread_id]["authentication"] = "Unknown"
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    if not history_data or history_entry != history_data[-1]:
                        history_data.append(history_entry)
                has_reconnected = False

            prev_auth_status = auth_status
            last_auth_status = auth_status
            company_fetched = True if auth_status == 0 else False
            last_activity = time.time()
            time.sleep(REQUEST_INTERVAL)
            gc.collect()
        except Exception as e:
            logging.error(f"Thread {thread_id}: Loop error, resetting: {e}")
            if sock:
                sock.close()
                sock = None
            if connection:
                try:
                    connection.disconnect()
                except:
                    pass
                connection = None
            time.sleep(5)
            gc.collect()
            continue

    if sock:
        sock.close()
        logging.info(f"Thread {thread_id}: Socket closed")
    if connection:
        try:
            connection.disconnect()
            logging.info(f"Thread {thread_id}: Reader disconnected")
        except Exception as e:
            logging.error(f"Thread {thread_id}: Error disconnecting reader: {e}")
    gc.collect()
    
def stop_processing():
    """Stop all processing threads and reset state."""
    global is_running, threads
    with data_lock:
        if not is_running:
            logging.warning("Processing not running, skipping stop")
            return
        is_running = False
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=5)
                if thread.is_alive():
                    logging.warning(f"Thread for reader {threads.index(thread)} failed to join, forcing termination")
        threads = []
        for i in range(NUM_CARDS):
            reader_data[i] = {
                "readerIndex": i,
                "status": "Disconnected",
                "companyName": "N/A",
                "atr": "N/A",
                "authentication": "Unknown",
                "presentTime": "N/A",
                "cardInsertTime": None
            }
        history_data.clear()
        logging.info(f"Stopped processing for all readers at {time.strftime('%H:%M:%S', time.localtime())}")
        gc.collect()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/readers')
def get_readers():
    try:
        with data_lock:
            data = [{k: v for k, v in reader.items() if k != "cardInsertTime"} for reader in reader_data.values()]
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        logging.error(f"Error in get_readers at {time.strftime('%H:%M:%S', time.localtime())}: {e}")
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
                return jsonify({"status": "success", "data": list(history_data)})
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
        data = request.get_json()
        if data is None:
            logging.error(f"Login failed at {time.strftime('%H:%M:%S', time.localtime())}: No JSON data received")
            return jsonify({"status": "error", "message": "Request must be application/json"}), 415

        username = data.get('username')
        password = data.get('password')

        if username == 'techvezoto' and password == 'techvezoto@1122':
            if start_processing():
                logging.info(f"Login successful at {time.strftime('%H:%M:%S', time.localtime())}, processing started")
                return jsonify({"status": "success"})
            else:
                logging.error(f"Login failed at {time.strftime('%H:%M:%S', time.localtime())}: Processing failed to start")
                return jsonify({"status": "error", "message": "Failed to start reader processing"}), 500

        logging.warning(f"Login failed at {time.strftime('%H:%M:%S', time.localtime())}: Invalid credentials")
        return jsonify({"status": "error", "message": "Invalid username or password"}), 401
    except Exception as e:
        logging.error(f"Error in login at {time.strftime('%H:%M:%S', time.localtime())}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/logout', methods=['POST'])
def logout():
    try:
        stop_processing()
        logging.info(f"Logout successful at {time.strftime('%H:%M:%S', time.localtime())}, processing stopped")
        return jsonify({"status": "success"})
    except Exception as e:
        logging.error(f"Error in logout at {time.strftime('%H:%M:%S', time.localtime())}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/graphs.html')
def graphs():
    return render_template('graphs.html')

@app.route('/logs.html')
def logs():
    return render_template('logs.html')

@socketio.on('connect', namespace='/logs')
def handle_connect():
    logging.info(f"Client connected to /logs namespace at {time.strftime('%H:%M:%S', time.localtime())}")

@socketio.on('disconnect', namespace='/logs')
def handle_disconnect():
    logging.info(f"Client disconnected from /logs namespace at {time.strftime('%H:%M:%S', time.localtime())}")

if __name__ == "__main__":
    gc.set_threshold(500, 5, 5)  # Adjusted for 981Mi total memory
    start_processing()  # Start processing on boot
    socketio.run(app, debug=False, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
