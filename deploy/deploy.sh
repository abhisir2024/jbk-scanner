#!/bin/bash
# JBK Scanner - Deploy Script
# Run this from your local machine

REMOTE_USER="ubuntu"
REMOTE_HOST="YOUR_ORACLE_IP"
REMOTE_DIR="/home/$REMOTE_USER/jbk-scanner"

echo "=== Deploying JBK Scanner to Oracle Cloud ==="

# Upload files
echo "Uploading files..."
scp -r "../scanner" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -r "../auth" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -r "../data" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -r "../alerts" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp -r "../static" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp "../dashboard.py" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp "../auto_scan.py" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp "../watchlist.py" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp "../requirements.txt" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp "../Dockerfile" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
scp "../docker-compose.yml" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"

# Build and start
echo "Building and starting..."
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_DIR && docker-compose up -d --build"

echo "=== Deployment Complete ==="
echo "Access your scanner at: http://$REMOTE_HOST:5001/"
