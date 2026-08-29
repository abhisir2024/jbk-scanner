#!/bin/bash
# JBK Scanner - Oracle Cloud Setup Script
# Run this on your Oracle Cloud VM

echo "=== JBK Scanner - Oracle Cloud Setup ==="

# Update system
sudo apt-get update && sudo apt-get upgrade -y

# Install Docker
sudo apt-get install -y docker.io docker-compose
sudo systemctl enable docker
sudo systemctl start docker

# Add user to docker group
sudo usermod -aG docker $USER

# Create project directory
mkdir -p ~/jbk-scanner
cd ~/jbk-scanner

echo "=== Setup Complete ==="
echo "Next steps:"
echo "1. Upload your scanner files to ~/jbk-scanner/"
echo "2. Run: docker-compose up -d"
echo "3. Access at: http://YOUR-IP:5001/"
