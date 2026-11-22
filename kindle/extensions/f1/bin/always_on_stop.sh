#!/bin/sh
# 停止常显模式并恢复正常睡眠行为(撤掉防入睡标志、拉回 Wi-Fi)
PIDFILE=/var/run/f1dash_alwayson.pid

if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
fi
pkill -f "f1dash.py alwayson" 2>/dev/null
# 恢复 Wi-Fi(常显收尾会硬关:stop wifid + ifconfig down;守护死后没人开它,
# 必须在这里拉回,否则设备离线)。顺序同守护侧 wifi(True);lipc set 在冷启
# wifid 源注册前会撞 NoSuchSource,故先 sleep 2 再设,失败也不阻塞——冷启
# wifid 自带 enable=1 会自己回连。
/sbin/ifconfig wlan0 up 2>/dev/null || ifconfig wlan0 up 2>/dev/null
/sbin/start wifid 2>/dev/null || start wifid 2>/dev/null
sleep 2
lipc-set-prop com.lab126.wifid enable 1 2>/dev/null
# 恢复 GUI 框架与状态栏渲染器(KUAL/正常阅读回来;守护已完全脱离会话,pkill 能抓到它)
/sbin/start framework 2>/dev/null || start framework 2>/dev/null
/sbin/start pillow 2>/dev/null || start pillow 2>/dev/null
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
echo "f1dash alwayson stopped"
