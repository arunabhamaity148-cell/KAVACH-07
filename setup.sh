#!/bin/bash

# KAVACH-07 v7.0.0 — Environment Setup Script
# Designed for Ubuntu 22.04+ (Oracle Cloud Free Tier)

set -e

echo "🛡️ Starting KAVACH-07 Setup..."

# 1. Update System
echo "Checking for system updates..."
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv sqlite3

# 2. Create Project Structure
echo "Configuring directories..."
mkdir -p logs
touch logs/kavach.log

# 3. Setup Virtual Environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# 4. Install Dependencies
echo "Installing dependencies from requirements.txt..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 5. Initialize Database
if [ ! -f "kavach.db" ]; then
    echo "Initializing SQLite database..."
    # Create empty DB file so schema can be applied by the bot on first run
    touch kavach.db
fi

# 6. Setup Permissions
chmod +x setup.sh

# 7. Check for .env
if [ ! -f ".env" ]; then
    echo "⚠️ WARNING: .env file not found!"
    echo "Please copy .env.example to .env and fill in your API keys."
    cp .env.example .env
fi

echo "✅ Setup Complete."
echo "To start the bot manually: ./venv/bin/python kavach/main.py"
echo "To install as a service: sudo cp kavach.service /etc/systemd/system/ && sudo systemctl enable kavach"