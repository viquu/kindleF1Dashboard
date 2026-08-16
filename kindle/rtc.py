#!/mnt/us/python3/bin/python3.9
# -*- coding: utf-8 -*-
"""RTC 闹钟设置(PW1 5.6.1.1)。

框架的 rtcWakeup 走的就是 /dev/rtc0 的 ioctl 机制
(RTC_WKALM_SET + RTC_AIE_ON),这里用 Python 直接调用,
用于 service 模式的定时唤醒刷新。

用法:
    /mnt/us/python3/bin/python3.9 /mnt/us/f1dash/rtc.py [秒数]
"""

import fcntl
import os
import struct
import sys
import time

RTC_AIE_ON = 0x7001
# _IOW('p', 0x0f, struct rtc_wkalrm)
# struct rtc_wkalrm = 4 字节头部 + struct rtc_time(9 个 int)
RTC_WKALM_SET = 0x4024700f


def set_alarm(seconds, dev="/dev/rtc0"):
    """设置 seconds 秒后触发一次 RTC 闹钟(一次性)。"""
    target = time.gmtime(time.time() + seconds)
    pkt = struct.pack("BBBB9i",
                      1, 0, 0, 0,               # enabled=1, pending=0, 保留
                      target.tm_sec, target.tm_min, target.tm_hour,
                      target.tm_mday, target.tm_mon, target.tm_year - 1900,
                      0, 0, 0)                  # wday, yday, isdst
    fd = os.open(dev, os.O_RDWR)
    try:
        fcntl.ioctl(fd, RTC_WKALM_SET, pkt)
        fcntl.ioctl(fd, RTC_AIE_ON)
    finally:
        os.close(fd)
    return target


def clear_alarm(dev="/dev/rtc0"):
    """清除闹钟(RTC_AIE_OFF)。"""
    fd = os.open(dev, os.O_RDWR)
    try:
        fcntl.ioctl(fd, 0x7002)  # RTC_AIE_OFF
    finally:
        os.close(fd)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    tgt = set_alarm(n)
    print("alarm set: now +%ds -> %02d:%02d:%02d UTC"
          % (n, tgt.tm_hour, tgt.tm_min, tgt.tm_sec))
