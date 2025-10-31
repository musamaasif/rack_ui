**Overview**
This project implements a Smart Card Rack System using OrangePi as the main MPU. The system monitors multiple smart card readers, communicates with a remote authentication server and displays real-time card activity, authentication states, and company details via a web-based dashboard.

It supports:
1. Multiple concurrent readers
2. Live socket-based logging
3. Authentication APDU command exchange
4. Historical tracking & CSV export
5. Allow to download Trackers data after successful authentication

**Core Features**
**Backend (server.py):**
1. Built with Flask and Flask-SocketIO
2. Manages multi-threaded smart card communication
3. Sends APDU commands, handles ATR parsing, and maintains live connection with the server

**Tracks:**
1. Reader status (Connected, Disconnected, Card Removed)
2. Authentication state (Authenticated, Failed, etc.)
3. Company names linked to cards
4. Card presence duration
5. Logs every operation to both console and SocketIO feed in real-time

**Includes:**
1. /readers --- JSON endpoint for real-time data
2. /history --- Returns log history
3. /download_history --- CSV export of reader history
4. /login and /logout --- Basic user authentication for dashboard access

**Frontend:**
The UI includes three major pages:
1. Dashboard(index.html) --- Displays live reader data with login screen and dynamic updates every second.
2. Graphs(graphs.html) --- Visual analytics of reader status, authentication outcomes, and company distribution via Chart.js.
3. Logs(logs.html) --- Real-time log viewer using Socket.IO with filters by reader and date.

**Developed For**
TechVezoto — Smart Rack Management & Authentication Systems
