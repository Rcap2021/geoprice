 cd /root/projects/geoprice/geoprice/backend
  nohup python3.10 deal_scanner.py > /tmp/deal_scanner.log 2>&1 &
  tail -f /tmp/deal_scanner.log

