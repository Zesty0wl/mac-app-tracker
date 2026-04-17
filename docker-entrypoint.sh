#!/bin/bash
set -e

echo "Starting ${SITE_NAME:-Mac Apps Version Tracker}"

# Function to handle shutdown gracefully
cleanup() {
    echo "Shutting down..."
    if [ -n "$SCHEDULER_PID" ]; then
        kill -TERM "$SCHEDULER_PID" 2>/dev/null || true
        wait "$SCHEDULER_PID" 2>/dev/null || true
    fi
    kill -TERM "$GUNICORN_PID" 2>/dev/null || true
    if [ -n "$GUNICORN_PID" ]; then
        wait "$GUNICORN_PID" 2>/dev/null || true
    fi
    exit 0
}

trap cleanup SIGTERM SIGINT

# Check if this is a dev environment (scheduler disabled in dev)
if [ "${DEV_MODE}" = "true" ]; then
    echo "Running in DEV mode - scheduler is DISABLED"
    SCHEDULER_PID=""
else
    # Start the scheduler in the background
    echo "Starting scheduler (runs check every hour)..."
    python3 scheduler.py &
    SCHEDULER_PID=$!
    echo "Scheduler PID: $SCHEDULER_PID"
    
    # Give the scheduler a moment to start
    sleep 2
fi

# Start the web server in the background
echo "Starting web server on port 5000..."
gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 300 web_app:app &
GUNICORN_PID=$!
echo "Gunicorn PID: $GUNICORN_PID"

# Monitor both processes
while true; do
    # Check if scheduler is still running (skip if in dev mode)
    if [ -n "$SCHEDULER_PID" ] && ! kill -0 "$SCHEDULER_PID" 2>/dev/null; then
        echo "ERROR: Scheduler died! Restarting..."
        python3 scheduler.py &
        SCHEDULER_PID=$!
        echo "Scheduler restarted with PID: $SCHEDULER_PID"
    fi
    
    # Check if gunicorn is still running
    if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
        echo "ERROR: Gunicorn died! Exiting..."
        exit 1
    fi
    
    sleep 30
done
