#!/bin/bash
# Start Muziek-Analyse server met Cloudflare Tunnel
# Gebruik: ./start-online.sh
# Stop:    ./start-online.sh stop

DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/.server.pid"
TUNNEL_PID_FILE="$DIR/.tunnel.pid"
LOG_FILE="$DIR/.tunnel.log"

stop_all() {
    echo "Stoppen..."
    if [ -f "$PID_FILE" ]; then
        kill $(cat "$PID_FILE") 2>/dev/null
        rm "$PID_FILE"
        echo "  Flask server gestopt"
    fi
    if [ -f "$TUNNEL_PID_FILE" ]; then
        kill $(cat "$TUNNEL_PID_FILE") 2>/dev/null
        rm "$TUNNEL_PID_FILE"
        echo "  Cloudflare tunnel gestopt"
    fi
    # Clean up any orphans
    lsof -i :5050 -t 2>/dev/null | xargs kill -9 2>/dev/null
    pkill -f "cloudflared tunnel --url http://localhost:5050" 2>/dev/null
    echo "Klaar."
}

if [ "$1" = "stop" ]; then
    stop_all
    exit 0
fi

# Stop any existing instances first
stop_all 2>/dev/null

cd "$DIR"

# Activate venv and start Flask
echo "Flask server starten op poort 5050..."
source .venv/bin/activate
FLASK_DEBUG=1 python app.py &>/tmp/flask-server.log &
echo $! > "$PID_FILE"
sleep 2

# Verify Flask is running
if ! curl -s http://localhost:5050/ > /dev/null 2>&1; then
    echo "FOUT: Flask server kon niet starten. Check /tmp/flask-server.log"
    exit 1
fi
echo "  Flask draait (PID $(cat "$PID_FILE"))"

# Start Cloudflare tunnel
echo "Cloudflare tunnel starten..."
cloudflared tunnel --url http://localhost:5050 2>"$LOG_FILE" &
echo $! > "$TUNNEL_PID_FILE"

# Wait for the tunnel URL
for i in $(seq 1 15); do
    URL=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -1)
    if [ -n "$URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$URL" ]; then
    echo "FOUT: Kon geen tunnel-URL verkrijgen. Check $LOG_FILE"
    exit 1
fi

echo ""
echo "================================================"
echo "  Muziek-Analyse is ONLINE!"
echo ""
echo "  Publieke URL: $URL"
echo ""
echo "  Lokaal:       http://localhost:5050"
echo "================================================"
echo ""
echo "Stop met: ./start-online.sh stop"
echo ""

# Copy URL to clipboard on macOS
echo "$URL" | pbcopy 2>/dev/null && echo "(URL gekopieerd naar klembord)"
