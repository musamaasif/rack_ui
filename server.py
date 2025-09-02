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

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

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

# Console handler for terminal output
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(formatter)

# SocketIO handler for streaming to logs.html
socketio_handler = SocketIOHandler()
socketio_handler.setLevel(logging.DEBUG)
socketio_handler.setFormatter(formatter)

# Add handlers to root logger
logging.getLogger('').addHandler(console_handler)
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
REQUEST_INTERVAL = 1
NUM_CARDS = 16
SOCKET_RETRY_INTERVAL = 5  # Seconds between retry attempts
SOCKET_RETRY_TIMEOUT = 60000  # Total retry duration in seconds
READER_INDEX_MAPPING = {}

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
        logging.info(f"Thread {reader_index}: Attempting to connect to reader: {reader_name} (system index {mapped_index}, mapped to reader {reader_index})")
        connection = reader_list[mapped_index].createConnection()
        observer = ConsoleCardConnectionObserver()
        connection.addObserver(observer)
        connection.connect()
        atr = toHexString(connection.getATR())
        logging.info(f"Thread {reader_index}: Connected to reader: {reader_name} (system index {mapped_index}, mapped to reader {reader_index}) with ATR: {atr}")
        return connection
    except Exception as e:
        logging.error(f"Thread {reader_index}: Reader connection error: {e}")
        return None

def execute_apdu(connection, apdu, thread_id):
    """Execute an APDU command and return response data and status."""
    try:
        data, sw1, sw2 = connection.transmit(toBytes(apdu))
        status = f"{sw1:02X}{sw2:02X}"
        logging.debug(f"Thread {thread_id}: APDU {apdu} response: {toHexString(data)}, status: {status}")
        return toHexString(data).replace(" ", ""), status
    except Exception as e:
        logging.error(f"Thread {thread_id}: APDU execution error for {apdu}: {e}")
        return None, None

def create_socket(thread_id):
    """Create and connect a TCP socket to the server with retries for 1 minute."""
    start_time = time.time()
    while time.time() - start_time < SOCKET_RETRY_TIMEOUT:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((SERVER_IP, SERVER_PORT))
            print(f"Thread {thread_id}: Connected to server {SERVER_IP}:{SERVER_PORT}")
            return sock
        except socket.error as e:
            print(f"Thread {thread_id}: Socket connection error: {e}. Retrying in {SOCKET_RETRY_INTERVAL} seconds...")
            time.sleep(SOCKET_RETRY_INTERVAL)
    print(f"Thread {thread_id}: Failed to connect to server after {SOCKET_RETRY_TIMEOUT} seconds")
    return None

def send_receive(sock, payload, operation, thread_id):
    """Send a payload to the server and receive the response."""
    try:
        sock.sendall(payload)
        logging.debug(f"Thread {thread_id}: Sent payload for {operation}: {payload.decode()}")
        response = sock.recv(4096).decode().strip()
        json_start = response.find('{')
        if json_start == -1:
            logging.error(f"Thread {thread_id}: No JSON found in {operation} response: {response}")
            return None
        json_data = response[json_start:]
        try:
            return json.loads(json_data)
        except json.JSONDecodeError as e:
            logging.error(f"Thread {thread_id}: JSON parsing error for {operation}: {e}, Response: {json_data}")
            return None
    except socket.error as e:
        logging.error(f"Thread {thread_id}: Socket error during {operation}: {e}")
        return None

def send_identifier(sock, identifier, thread_id, vehicle_schedule_id=None):
    """Send identifier to server and return response data."""
    payload_data = {"atr": identifier, "app_id": APP_ID, "reader_no": thread_id + 1}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "atr", "data": payload_data}).encode()
    return send_receive(sock, payload, "send_identifier", thread_id)

def fetch_company_name(sock, identifier, thread_id, vehicle_schedule_id=None):
    """Fetch company name from server using get_company_card message."""
    payload_data = {"atr": identifier, "reader_no": thread_id + 1,"app_id": APP_ID,}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": "get_company_card", "data": payload_data}).encode()
    response_data = send_receive(sock, payload, "fetch_company_name", thread_id)
    if response_data and isinstance(response_data.get("data"), dict):
        company_name = response_data["data"].get(identifier.lower())
        if company_name:
            logging.info(f"Thread {thread_id}: Received company name: {company_name}")
            return company_name
        logging.warning(f"Thread {thread_id}: No company name received for ATR {identifier}, response: {response_data}")
    else:
        logging.error(f"Thread {thread_id}: Invalid response from fetch_company_name: {response_data}")
    return None

def send_card_status(sock, identifier, thread_id, status, vehicle_schedule_id=None):
    """Send card status (inserted/removed) to server and log the response."""
    payload_data = {"atr": identifier, "reader_no": thread_id + 1,"app_id": APP_ID,}
    if vehicle_schedule_id is not None:
        payload_data["vehicle_schedule_id"] = vehicle_schedule_id
    payload = json.dumps({"type": f"card_{status}" , "data": payload_data}).encode()
    response_data = send_receive(sock, payload, f"send_card_status_{status}", thread_id)
    if response_data:
        logging.info(f"Thread {thread_id}: Card {status} payload sent successfully: {response_data}")
    else:
        logging.error(f"Thread {thread_id}: Failed to send card {status} payload")
    return response_data

def fetch_apdu_from_server(sock, identifier, thread_id, response_data=None, status=None, pre_apdu=None, vehicle_schedule_id=None):
    """Fetch APDU from server for authentication."""
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
            logging.debug(f"Thread {thread_id}: Received APDU: {data_apdu}")
            return data_apdu
        logging.error(f"Thread {thread_id}: No APDU received or invalid response data format")
    return None

history_data = []
HISTORY_LIMIT = 1000
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

    try:
        while is_running:
            if not connection:
                connection = connect_reader(reader_index)
                if not connection:
                    with data_lock:
                        # Store history when reader disconnects
                        history_entry = {
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        }
                        history_data.append(history_entry)
                        if len(history_data) > HISTORY_LIMIT:
                            history_data.pop(0)
                        reader_data[reader_index]["status"] = "Disconnected"
                        reader_data[reader_index]["presentTime"] = "N/A"
                        reader_data[reader_index]["cardInsertTime"] = None
                        reader_data[reader_index]["atr"] = "N/A"
                        reader_data[reader_index]["companyName"] = "N/A"
                        reader_data[reader_index]["authentication"] = "Unknown"
                    company_name = None
                    time.sleep(REQUEST_INTERVAL)
                    continue

                identifier_parts = []
                for i, apdu in enumerate(APDU_COMMANDS):
                    data, status = execute_apdu(connection, apdu, thread_id)
                    if data is None or status != "9000":
                        logging.error(f"Thread {thread_id}: APDU {apdu} failed with status: {status}")
                        with data_lock:
                            # Store history on APDU failure
                            history_entry = {
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index,
                                "status": "Disconnected",
                                "companyName": "N/A",
                                "atr": "N/A",
                                "authentication": "Unknown",
                                "presentTime": "N/A"
                            }
                            history_data.append(history_entry)
                            if len(history_data) > HISTORY_LIMIT:
                                history_data.pop(0)
                            reader_data[thread_id]["status"] = "Disconnected"
                            reader_data[thread_id]["presentTime"] = "N/A"
                            reader_data[thread_id]["cardInsertTime"] = None
                            reader_data[thread_id]["companyName"] = "N/A"
                        connection.disconnect()
                        connection = None
                        company_name = None
                        time.sleep(REQUEST_INTERVAL)
                        break
                    if i in [1, 3]:
                        identifier_parts.append(data)

                if len(identifier_parts) != 2:
                    logging.error(f"Thread {thread_id}: Failed to collect both identifier parts")
                    with data_lock:
                        # Store history on identifier failure
                        history_entry = {
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        }
                        history_data.append(history_entry)
                        if len(history_data) > HISTORY_LIMIT:
                            history_data.pop(0)
                        reader_data[thread_id]["status"] = "Disconnected"
                        reader_data[thread_id]["presentTime"] = "N/A"
                        reader_data[thread_id]["cardInsertTime"] = None
                        reader_data[thread_id]["companyName"] = "N/A"
                    connection.disconnect()
                    connection = None
                    company_name = None
                    time.sleep(REQUEST_INTERVAL)
                    continue

                combined_identifier = "".join(identifier_parts)
                with data_lock:
                    # Store history on successful connection
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": "Connected",
                        "companyName": "N/A",
                        "atr": combined_identifier,
                        "authentication": "Unknown",
                        "presentTime": format_duration(0)
                    }
                    history_data.append(history_entry)
                    if len(history_data) > HISTORY_LIMIT:
                        history_data.pop(0)
                    reader_data[thread_id]["status"] = "Connected"
                    reader_data[thread_id]["atr"] = combined_identifier
                    reader_data[thread_id]["presentTime"] = format_duration(0)
                    reader_data[thread_id]["cardInsertTime"] = time.time()
                    reader_data[thread_id]["companyName"] = "N/A"

                sock = create_socket(thread_id)
                if not sock:
                    with data_lock:
                        # Store history on socket failure
                        history_entry = {
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        }
                        history_data.append(history_entry)
                        if len(history_data) > HISTORY_LIMIT:
                            history_data.pop(0)
                        reader_data[thread_id]["status"] = "Disconnected"
                        reader_data[thread_id]["presentTime"] = "N/A"
                        reader_data[thread_id]["cardInsertTime"] = None
                        reader_data[thread_id]["companyName"] = "N/A"
                    connection.disconnect()
                    connection = None
                    company_name = None
                    time.sleep(REQUEST_INTERVAL)
                    continue

                # Send card inserted status
                send_card_status(sock, combined_identifier, thread_id, "inserted", vehicle_schedule_id)

            with data_lock:
                if reader_data[thread_id]["cardInsertTime"]:
                    reader_data[thread_id]["presentTime"] = format_duration(time.time() - reader_data[thread_id]["cardInsertTime"])
                    # Store history on present time update
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    history_data.append(history_entry)
                    if len(history_data) > HISTORY_LIMIT:
                        history_data.pop(0)
                else:
                    reader_data[thread_id]["presentTime"] = "N/A"

            try:
                connection.getATR()
            except Exception as e:
                logging.error(f"Thread {thread_id}: Error during getATR: {e}")
                # Send card removed status
                if sock and combined_identifier:
                    send_card_status(sock, combined_identifier, thread_id, "removed", vehicle_schedule_id)
                with data_lock:
                    # Store history on card removal
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": "Card Removed",
                        "companyName": "N/A",
                        "atr": "N/A",
                        "authentication": "Unknown",
                        "presentTime": "N/A"
                    }
                    history_data.append(history_entry)
                    if len(history_data) > HISTORY_LIMIT:
                        history_data.pop(0)
                    reader_data[thread_id]["status"] = "Card Removed"
                    reader_data[thread_id]["presentTime"] = "N/A"
                    reader_data[thread_id]["cardInsertTime"] = None
                    reader_data[thread_id]["atr"] = "N/A"
                    reader_data[thread_id]["authentication"] = "Unknown"
                    reader_data[thread_id]["companyName"] = "N/A"
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

            response_data = send_identifier(sock, combined_identifier, thread_id, vehicle_schedule_id)
            if not response_data:
                logging.error(f"Thread {thread_id}: Failed to retrieve server response, attempting to reconnect")
                sock.close()
                sock = create_socket(thread_id)
                if not sock:
                    with data_lock:
                        # Store history on socket reconnect failure
                        history_entry = {
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        }
                        history_data.append(history_entry)
                        if len(history_data) > HISTORY_LIMIT:
                            history_data.pop(0)
                        reader_data[thread_id]["status"] = "Disconnected"
                        reader_data[thread_id]["presentTime"] = "N/A"
                        reader_data[thread_id]["cardInsertTime"] = None
                        reader_data[thread_id]["companyName"] = "N/A"
                    connection.disconnect()
                    connection = None
                    company_name = None
                    combined_identifier = None
                    time.sleep(REQUEST_INTERVAL)
                    continue
                time.sleep(REQUEST_INTERVAL)
                continue

            if isinstance(response_data.get("data"), dict):
                vehicle_schedule_id = response_data["data"].get("vehicle_schedule_id")
                if vehicle_schedule_id:
                    logging.debug(f"Thread {thread_id}: Received vehicle_schedule_id: {vehicle_schedule_id}")

            auth_status = response_data.get("data", {}).get(combined_identifier.lower(), -1)
            with data_lock:
                reader_data[thread_id]["authentication"] = (
                    "No Authentication Required" if auth_status == 0 else
                    "Authentication Required" if auth_status == 1 else
                    f"Authentication Failed ({auth_status})" if auth_status > 1 else
                    "Unknown"
                )
                # Store history on authentication status update
                history_entry = {
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                    "readerIndex": reader_index,
                    "status": reader_data[thread_id]["status"],
                    "companyName": reader_data[thread_id]["companyName"],
                    "atr": reader_data[thread_id]["atr"],
                    "authentication": reader_data[thread_id]["authentication"],
                    "presentTime": reader_data[thread_id]["presentTime"]
                }
                history_data.append(history_entry)
                if len(history_data) > HISTORY_LIMIT:
                    history_data.pop(0)

            if auth_status == 1:
                logging.info(f"Thread {thread_id}: Authentication required")
                with data_lock:
                    reader_data[thread_id]["status"] = "Connected"
                    # Store history on status update
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    history_data.append(history_entry)
                    if len(history_data) > HISTORY_LIMIT:
                        history_data.pop(0)
                data_apdu = fetch_apdu_from_server(sock, combined_identifier, thread_id, vehicle_schedule_id=vehicle_schedule_id)
                if not data_apdu:
                    logging.error(f"Thread {thread_id}: Failed to fetch APDU")
                    time.sleep(REQUEST_INTERVAL)
                    continue
                apdu = data_apdu.get('apdu')
                vehicle_schedule_id = data_apdu.get('vehicle_schedule_id', vehicle_schedule_id)

                while apdu and apdu != "00000000000000":
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
                        logging.error(f"Thread {thread_id}: Authentication APDU {apdu} failed with status: {status}")
                        with data_lock:
                            # Store history on APDU failure
                            history_entry = {
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index,
                                "status": "Disconnected",
                                "companyName": "N/A",
                                "atr": "N/A",
                                "authentication": "Unknown",
                                "presentTime": "N/A"
                            }
                            history_data.append(history_entry)
                            if len(history_data) > HISTORY_LIMIT:
                                history_data.pop(0)
                            reader_data[thread_id]["status"] = "Disconnected"
                            reader_data[thread_id]["presentTime"] = "N/A"
                            reader_data[thread_id]["cardInsertTime"] = None
                            reader_data[thread_id]["companyName"] = "N/A"
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
                            # Store history on successful authentication
                            history_entry = {
                                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                "readerIndex": reader_index,
                                "status": "Connected",
                                "companyName": "N/A",
                                "atr": reader_data[thread_id]["atr"],
                                "authentication": "No Authentication Required",
                                "presentTime": format_duration(0)
                            }
                            history_data.append(history_entry)
                            if len(history_data) > HISTORY_LIMIT:
                                history_data.pop(0)
                            reader_data[thread_id]["status"] = "Connected"
                            reader_data[thread_id]["authentication"] = "No Authentication Required"
                            reader_data[thread_id]["cardInsertTime"] = time.time()
                            reader_data[thread_id]["presentTime"] = format_duration(0)
                            reader_data[thread_id]["companyName"] = "N/A"
                        connection.disconnect()
                        connection = None
                        connection = connect_reader(reader_index)
                        if not connection:
                            with data_lock:
                                # Store history on reconnect failure
                                history_entry = {
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                                    "readerIndex": reader_index,
                                    "status": "Disconnected",
                                    "companyName": "N/A",
                                    "atr": "N/A",
                                    "authentication": "Unknown",
                                    "presentTime": "N/A"
                                }
                                history_data.append(history_entry)
                                if len(history_data) > HISTORY_LIMIT:
                                    history_data.pop(0)
                                reader_data[thread_id]["status"] = "Disconnected"
                                reader_data[thread_id]["presentTime"] = "N/A"
                                reader_data[thread_id]["cardInsertTime"] = None
                                reader_data[thread_id]["companyName"] = "N/A"
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
                    # Store history on no authentication required
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    history_data.append(history_entry)
                    if len(history_data) > HISTORY_LIMIT:
                        history_data.pop(0)
                company_name = fetch_company_name(sock, combined_identifier, thread_id, vehicle_schedule_id)
                with data_lock:
                    reader_data[thread_id]["companyName"] = company_name if company_name else "N/A"
                    # Store history on company name update
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    history_data.append(history_entry)
                    if len(history_data) > HISTORY_LIMIT:
                        history_data.pop(0)
                has_reconnected = False
            elif auth_status > 1 and not has_reconnected and prev_auth_status <= 1:
                logging.info(f"Thread {thread_id}: Authentication status changed to {auth_status} > 1, disconnecting and reconnecting")
                with data_lock:
                    # Store history on authentication failure
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": "Disconnected",
                        "companyName": "N/A",
                        "atr": "N/A",
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": "N/A"
                    }
                    history_data.append(history_entry)
                    if len(history_data) > HISTORY_LIMIT:
                        history_data.pop(0)
                    reader_data[thread_id]["status"] = "Disconnected"
                    reader_data[thread_id]["presentTime"] = "N/A"
                    reader_data[thread_id]["cardInsertTime"] = None
                    reader_data[thread_id]["companyName"] = "N/A"
                connection.disconnect()
                connection = connect_reader(reader_index)
                if not connection:
                    with data_lock:
                        # Store history on reconnect failure
                        history_entry = {
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                            "readerIndex": reader_index,
                            "status": "Disconnected",
                            "companyName": "N/A",
                            "atr": "N/A",
                            "authentication": "Unknown",
                            "presentTime": "N/A"
                        }
                        history_data.append(history_entry)
                        if len(history_data) > HISTORY_LIMIT:
                            history_data.pop(0)
                        reader_data[thread_id]["status"] = "Disconnected"
                        reader_data[thread_id]["presentTime"] = "N/A"
                        reader_data[thread_id]["cardInsertTime"] = None
                        reader_data[thread_id]["companyName"] = "N/A"
                    company_name = None
                    combined_identifier = None
                    time.sleep(REQUEST_INTERVAL)
                    continue
                has_reconnected = True
            elif auth_status > 1 and has_reconnected:
                logging.debug(f"Thread {thread_id}: Authentication status {auth_status} > 1, already reconnected")
            elif auth_status > 1 and prev_auth_status > 1:
                logging.debug(f"Thread {thread_id}: Authentication status {auth_status} > 1, no status change")
            else:
                with data_lock:
                    reader_data[thread_id]["authentication"] = "Unknown"
                    # Store history on unknown authentication
                    history_entry = {
                        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                        "readerIndex": reader_index,
                        "status": reader_data[thread_id]["status"],
                        "companyName": reader_data[thread_id]["companyName"],
                        "atr": reader_data[thread_id]["atr"],
                        "authentication": reader_data[thread_id]["authentication"],
                        "presentTime": reader_data[thread_id]["presentTime"]
                    }
                    history_data.append(history_entry)
                    if len(history_data) > HISTORY_LIMIT:
                        history_data.pop(0)
                has_reconnected = False

            prev_auth_status = auth_status
            time.sleep(REQUEST_INTERVAL)

    except Exception as e:
        logging.error(f"Thread {thread_id}: Unexpected error: {e}")
        with data_lock:
            # Store history on unexpected error
            history_entry = {
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                "readerIndex": reader_index,
                "status": "Disconnected",
                "companyName": "N/A",
                "atr": "N/A",
                "authentication": "Unknown",
                "presentTime": "N/A"
            }
            history_data.append(history_entry)
            if len(history_data) > HISTORY_LIMIT:
                history_data.pop(0)
            reader_data[thread_id]["status"] = "Disconnected"
            reader_data[thread_id]["presentTime"] = "N/A"
            reader_data[thread_id]["cardInsertTime"] = None
            reader_data[thread_id]["companyName"] = "N/A"
        company_name = None
        combined_identifier = None
    finally:
        if sock:
            sock.close()
            logging.info(f"Thread {thread_id}: Socket closed")
        if connection:
            try:
                connection.disconnect()
                logging.info(f"Thread {thread_id}: Reader disconnected")
            except Exception as e:
                logging.error(f"Thread {thread_id}: Error disconnecting reader: {e}")

def start_processing():
    global is_running, threads, READER_INDEX_MAPPING
    with data_lock:
        if not is_running:
            is_running = True
            try:
                system_readers = readers()
                READER_INDEX_MAPPING = {i: i for i in range(min(NUM_CARDS, len(system_readers)))}
                threads = [threading.Thread(target=process_card, args=(i,)) for i in range(NUM_CARDS)]
                for thread in threads:
                    thread.start()
                logging.info(f"Started processing for all readers at {time.strftime('%H:%M:%S', time.localtime())}")
            except Exception as e:
                logging.error(f"Failed to start processing at {time.strftime('%H:%M:%S', time.localtime())}: {e}")
                is_running = False
                threads = []
                return False
    return True

def stop_processing():
    global is_running, threads
    with data_lock:
        if is_running:
            is_running = False
            for thread in threads:
                thread.join()
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
        logging.error(f"Error in get_readers at {time.strftime('%H:%M:%S', time.localtime())}: {e}")
        return jsonify({"status": "error", "message": str(e), "data": []}), 500

@app.route('/history')
def get_history():
    try:
        reader_index = request.args.get('readerIndex', type=int)
        with data_lock:
            if reader_index is not None:
                # Only return history for the requested reader index
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
        logging.debug(f"Received login request at {time.strftime('%H:%M:%S', time.localtime())} with headers: {dict(request.headers)} and body: {request.get_data()}")
        data = request.get_json()

        if data is None:
            logging.error(f"Login failed at {time.strftime('%H:%M:%S', time.localtime())}: No JSON data received")
            return jsonify({"status": "error", "message": "Request must be application/json"}), 415

        username = data.get('username')
        password = data.get('password')

        # ✅ STATIC CREDENTIALS CHECK
        if username == 'techvezoto' and password == 'techvezoto@1122':
            if start_processing():
                logging.info(f"Login successful at {time.strftime('%H:%M:%S', time.localtime())}, processing started")
                return jsonify({"status": "success"})
            else:
                logging.error(f"Login failed at {time.strftime('%H:%M:%S', time.localtime())}: Processing failed to start")
                return jsonify({"status": "error", "message": "Failed to start reader processing"}), 500

        # ❌ Invalid credentials
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
    socketio.run(app, debug=False, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
