# Oracle Cloud Free Tier - JBK Scanner Deployment Guide

## Step 1: Create Oracle Cloud Account

1. Go to [cloud.oracle.com](https://cloud.oracle.com)
2. Click **Sign Up** or **Start for Free**
3. Fill in your details
4. **Important**: Choose **Always Free** tier (no credit card charged)
5. Verify your email

---

## Step 2: Create VM Instance

1. Log in to Oracle Cloud Console
2. Click **Create a VM Instance**
3. Configure:
   - **Name**: `jbk-scanner`
   - **Image**: Ubuntu 22.04 (or Oracle Linux)
   - **Shape**: VM.Standard.A1.Flex (Always Free eligible)
   - **Cores**: 4 OCPU
   - **Memory**: 24 GB RAM
   - **Boot Volume**: 200 GB
4. Click **Create**
5. **Save your SSH key** - you'll need it to connect

---

## Step 3: Connect to Your VM

### From Windows (PowerShell):
```bash
ssh -i your-key.pem ubuntu@YOUR-VM-IP
```

### From Mac/Linux:
```bash
chmod 400 your-key.pem
ssh -i your-key.pem ubuntu@YOUR-VM-IP
```

---

## Step 4: Install Docker on VM

```bash
# Update system
sudo apt-get update && sudo apt-get upgrade -y

# Install Docker
sudo apt-get install -y docker.io docker-compose

# Start Docker
sudo systemctl enable docker
sudo systemctl start docker

# Add user to docker group
sudo usermod -aG docker $USER

# Log out and back in for group changes
exit
```

---

## Step 5: Upload Your Scanner Files

### Option A: Using SCP (from your local machine)
```bash
# From your computer (E:\Fyers API)
scp -r scanner/ ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp -r auth/ ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp -r data/ ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp -r alerts/ ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp -r static/ ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp dashboard.py ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp auto_scan.py ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp requirements.txt ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp Dockerfile ubuntu@YOUR-VM-IP:~/jbk-scanner/
scp docker-compose.yml ubuntu@YOUR-VM-IP:~/jbk-scanner/
```

### Option B: Using FileZilla (easier)
1. Download FileZilla
2. Connect to your VM via SFTP
3. Upload entire `E:\Fyers API` folder to `/home/ubuntu/jbk-scanner/`

---

## Step 6: Build and Start

```bash
# SSH into your VM
ssh -i your-key.pem ubuntu@YOUR-VM-IP

# Navigate to project
cd ~/jbk-scanner

# Build and start
docker-compose up -d --build

# Check status
docker-compose ps

# View logs
docker-compose logs -f
```

---

## Step 7: Open Port 5001

1. Go to Oracle Cloud Console
2. Navigate to **Networking** → **Virtual Cloud Networks**
3. Click on your VCN
4. Click **Security Lists**
5. Click **Default Security List**
6. Click **Add Ingress Rules**
7. Add:
   - **Source CIDR**: 0.0.0.0/0
   - **Destination Port**: 5001
8. Click **Add Ingress Rules**

---

## Step 8: Access Your Scanner

Open browser and go to:
```
http://YOUR-VM-IP:5001/
```

---

## Daily Token Refresh

Your Fyers API token expires daily. You need to refresh it:

### Option 1: Manual Refresh (Daily)
```bash
# SSH into VM
ssh -i your-key.pem ubuntu@YOUR-VM-IP

# Run daily login
docker exec jbk-scanner python auth/daily_login.py
```

### Option 2: Auto Refresh (Recommended)
Add to crontab:
```bash
# Edit crontab
crontab -e

# Add this line (runs at 9:10 AM IST every day)
10 9 * * 1-5 docker exec jbk-scanner python auth/daily_login.py >> /var/log/scanner-cron.log 2>&1
```

---

## Useful Commands

```bash
# Check if scanner is running
docker-compose ps

# View logs
docker-compose logs -f

# Restart scanner
docker-compose restart

# Stop scanner
docker-compose down

# Update scanner (after code changes)
docker-compose up -d --build

# Access container shell
docker exec -it jbk-scanner bash

# Run manual scan
docker exec jbk-scanner python scan.py
```

---

## Troubleshooting

### Scanner won't start
```bash
# Check logs
docker-compose logs

# Common fix: rebuild
docker-compose down
docker-compose up -d --build
```

### Can't access from browser
1. Check Oracle Cloud security list (Step 7)
2. Check VM firewall:
```bash
sudo ufw allow 5001/tcp
sudo ufw reload
```

### Token expired
```bash
# Refresh token
docker exec jbk-scanner python auth/daily_login.py
```

---

## Cost

**Always Free Tier Includes:**
- 4 OCPU ARM-based VM
- 24 GB RAM
- 200 GB storage
- 10 TB outbound data

**You pay $0 forever** (as long as you stay within free tier limits)

---

## Security Tips

1. **Change default port** (optional): Edit `docker-compose.yml`
2. **Add authentication**: Add login page to dashboard
3. **Use HTTPS**: Set up Let's Encrypt (free SSL)
4. **Backup data**: Regular backups of `auth/` and `data/` folders

---

## Support

If you need help:
1. Check Oracle Cloud docs: [docs.oracle.com](https://docs.oracle.com)
2. Check Docker logs: `docker-compose logs`
3. Check scanner logs: `cat dashboard.log`
