# Smart Card Rack UI

## Overview
The Smart Card Rack UI is a web-based dashboard and backend service designed to manage, monitor, and map a physical rack of smart card readers. It helps to Authenticate Company Card with Tachograph and also provides a visual interface to track reader statuses and ensures predictable hardware mapping for automated operations.

## How App Works
The `server.py` file acts as the core backend application, typically running on a lightweight framework like Flask. Its primary responsibilities include:
* **Hardware Mapping:** It maintains a strict configuration (like `MANUAL_SERIAL_ORDER`) to map dynamically connected USB smart card readers (e.g., HID Global / OMNIKEY) to their exact physical slots in the rack.
* **Reader Sorting & Caching:** It utilizes functions like `get_readers_mapped()` to process the raw hardware data and return a consistently sorted list of readers, supporting both full names and serial numbers. 
* **Authentication:** It manages access control and security, ensuring that interactions with the smart card rack and its endpoints are restricted to authorized users or systems.
* **Web Serving:** It handles HTTP routing, rendering the frontend UI (from the `templates` and `static` directories) and serving it locally on port 5000.

## Deployment Documentation

### Prerequisites
* Any SBC/PC with Linux OS installed
* Internet connectivity
* Tailscale account (for remote SSH access)
* Ngrok account (for public HTTP tunneling)
* Python 3 installed
* `server.py` file present in the `rack_ui` folder

### Step-by-Step Instructions

#### 1. Install Tailscale on Orange Pi
Access your SBC using a monitor and keyboard, or via serial access, and install Tailscale:
```bash
curl -fsSL [https://tailscale.com/install.sh](https://tailscale.com/install.sh) | sh
```
#### 2. Start and Authenticate Tailscale
Start the Tailscale service:
```bash
sudo tailscale up
```

#### 3. Get SSH Access Using Tailscale
Once authenticated, your device will appear in your Tailscale dashboard. From your main computer, SSH into the device using its Tailscale IP address:
```bash
ssh orangepi@<tailscale-ip-address>
```

####4. Navigate to the Project Directory
Once logged into the Orange Pi via SSH, navigate to the folder containing your UI server:
```bash
cd ~/rack_ui
```

#### 5. Start the UI Server
Run the Python server in the background using nohup so it continues running after you close the terminal:
```bash
nohup python3 server.py > test.log 2>&1 &
```
Note: Log output will be saved to test.log.

#### 6. Start Ngrok for Public Access
Expose your local server (which runs on port 5000) to the internet using Ngrok:
```bash
nohup ngrok http --domain=pro-chigger-gradually.ngrok-free.app 5000 > ngrok.log 2>&1 &
```
This will tunnel the local server to the designated public domain.

#### 7. Access the Smart Card Rack UI
You can now access the interface from any browser using the following link:

https://pro-chigger-gradually.ngrok-free.app/

#### Useful Commands & Notes

Port Configuration: Ensure server.py is configured to run on port 5000, or update the Ngrok command accordingly.

Background Processes: If using Tailscale and Ngrok together, verify both are running in the background.

To check if your services are running:

```bash
ps aux | grep python
ps aux | grep ngrok
```
To stop a service:

Find the Process ID (PID) using the commands above, then kill it:
```bash
kill <pid>
```
