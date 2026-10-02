"""记录器：原始报文日志 + 解析后数据 CSV。"""

from __future__ import annotations

import csv
import os
import queue
import threading
import time
from datetime import datetime, timezone

CSV_COLUMNS = [
    'recorded_at', 'utc', 'date',
    'lat', 'lon', 'alt', 'geoid_sep', 'quality', 'quality_text', 'fix', 'num_sv', 'hdop',
    'heading_deg', 'heading_source', 'ths_deg', 'ths_status',
    'pitch_deg', 'baseline_length_m', 'sol_status', 'pos_type',
    'heading_stddev_deg', 'pitch_stddev_deg', 'heading_mismatch_deg',
    'speed_kn', 'speed_kmh', 'course_deg',
    'vel_n_mps', 'vel_e_mps', 'vel_u_mps',
    'sats_tracked', 'sats_used', 'max_cn0',
    # 从天线（Slave Antenna，H 后缀日志）自有解
    'slave_lat', 'slave_lon', 'slave_alt', 'slave_fix', 'slave_sats_used',
    'slave_speed_kn', 'slave_course_deg',
    'slave_std_lat', 'slave_std_lon', 'slave_std_alt',
]


class Recorder:
    def __init__(self, directory: str):
        self.directory = directory
        self.lock = threading.RLock()
        self.raw_handle = None
        self.raw_path: str | None = None
        self.csv_handle = None
        self.csv_writer = None
        self.csv_path: str | None = None
        self.csv_interval = 0.2
        self.csv_rows = 0
        self.raw_lines = 0
        self.raw_dropped = 0
        self._raw_queue: queue.Queue | None = None
        self._raw_thread: threading.Thread | None = None
        self._last_csv_at = 0.0
        os.makedirs(directory, exist_ok=True)

    RAW_QUEUE_SIZE = 20000

    # ------------------------------------------------------------------ #

    @staticmethod
    def _stamp() -> str:
        return datetime.now().strftime('%Y%m%d_%H%M%S')

    def _unique(self, stem: str, suffix: str) -> str:
        base = os.path.join(self.directory, f'{stem}_{self._stamp()}')
        candidate = f'{base}{suffix}'
        counter = 1
        while os.path.exists(candidate):
            candidate = f'{base}_{counter}{suffix}'
            counter += 1
        return candidate

    # ------------------------------------------------------------------ #

    def raw_active(self) -> bool:
        with self.lock:
            return self.raw_handle is not None

    def csv_active(self) -> bool:
        with self.lock:
            return self.csv_handle is not None

    def status(self) -> dict:
        with self.lock:
            return {
                'raw_active': self.raw_handle is not None,
                'raw_path': self.raw_path,
                'raw_lines': self.raw_lines,
                'raw_dropped': self.raw_dropped,
                'csv_active': self.csv_handle is not None,
                'csv_path': self.csv_path,
                'csv_rows': self.csv_rows,
                'csv_interval_s': self.csv_interval,
                'directory': self.directory,
            }

    def start_raw(self) -> str:
        with self.lock:
            if self.raw_handle is not None:
                return self.raw_path or ''
            self.raw_path = self._unique('raw', '.txt')
            # 带 BOM 写出，Windows 记事本/Excel 才能正确识别中文注释行
            self.raw_handle = open(self.raw_path, 'w', encoding='utf-8-sig', newline='\n')
            self.raw_lines = 0
            self.raw_dropped = 0
            self.raw_handle.write(f'# GNSS 原始报文日志  开始于 {datetime.now().isoformat(timespec="seconds")}\n')
            self.raw_handle.flush()

            # 串口读线程只入队、不碰磁盘：磁盘抖动（杀毒扫描/网络盘）不该阻塞收数
            self._raw_queue = queue.Queue(maxsize=self.RAW_QUEUE_SIZE)
            self._raw_thread = threading.Thread(
                target=self._raw_writer_loop, args=(self._raw_queue,), name='raw-writer', daemon=True,
            )
            self._raw_thread.start()
            return self.raw_path

    def _raw_writer_loop(self, channel: queue.Queue) -> None:
        pending = 0
        while True:
            try:
                line = channel.get(timeout=0.5)
            except queue.Empty:
                with self.lock:
                    handle = self.raw_handle
                if handle is not None:
                    try:
                        handle.flush()
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if line is None:
                break
            with self.lock:
                handle = self.raw_handle
                if handle is None:
                    continue
                try:
                    handle.write(line + '\n')
                    self.raw_lines += 1
                    pending += 1
                    if pending >= 200:
                        handle.flush()
                        pending = 0
                except Exception:  # noqa: BLE001
                    break
        with self.lock:
            if self.raw_handle is not None:
                try:
                    self.raw_handle.flush()
                except Exception:  # noqa: BLE001
                    pass

    def start_csv(self, interval: float = 0.2) -> str:
        with self.lock:
            if self.csv_handle is not None:
                return self.csv_path or ''
            self.csv_path = self._unique('data', '.csv')
            self.csv_handle = open(self.csv_path, 'w', encoding='utf-8-sig', newline='')
            self.csv_writer = csv.DictWriter(self.csv_handle, fieldnames=CSV_COLUMNS)
            self.csv_writer.writeheader()
            self.csv_handle.flush()
            self.csv_interval = max(0.02, float(interval))
            self.csv_rows = 0
            self._last_csv_at = 0.0
            return self.csv_path

    def stop_raw(self) -> None:
        with self.lock:
            channel = self._raw_queue
            thread = self._raw_thread
            self._raw_queue = None
            self._raw_thread = None
        if channel is not None:
            # 先腾空队列，保证停止哨兵一定放得进去（否则 20k 积压时线程无法退出）
            while True:
                try:
                    channel.get_nowait()
                except queue.Empty:
                    break
            try:
                channel.put_nowait(None)
            except queue.Full:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with self.lock:
            if self.raw_handle is not None:
                try:
                    self.raw_handle.flush()
                    self.raw_handle.close()
                except Exception:  # noqa: BLE001
                    pass
            self.raw_handle = None

    def stop_csv(self) -> None:
        with self.lock:
            if self.csv_handle is not None:
                try:
                    self.csv_handle.flush()
                    self.csv_handle.close()
                except Exception:  # noqa: BLE001
                    pass
            self.csv_handle = None
            self.csv_writer = None

    def stop_all(self) -> None:
        self.stop_raw()
        self.stop_csv()

    # ------------------------------------------------------------------ #

    def write_raw(self, line: str) -> None:
        """把原始行交给写线程（串口读线程不做磁盘 I/O）。队列满时丢弃并计数。"""
        channel = self._raw_queue
        if channel is None:
            return
        try:
            channel.put_nowait(line)
        except queue.Full:
            self.raw_dropped += 1

    def maybe_write_csv(self, snapshot: dict) -> None:
        with self.lock:
            if self.csv_writer is None:
                return
            now = time.monotonic()
            if now - self._last_csv_at < self.csv_interval:
                return
            self._last_csv_at = now
            row = self._row_from(snapshot)
            try:
                self.csv_writer.writerow(row)
                self.csv_rows += 1
                if self.csv_rows % 20 == 0:
                    self.csv_handle.flush()
            except Exception:  # noqa: BLE001
                self.stop_csv()

    @staticmethod
    def _row_from(snapshot: dict) -> dict:
        position = snapshot.get('position') or {}
        heading = snapshot.get('heading') or {}
        log = heading.get('log') or {}
        ths = heading.get('ths') or {}
        velocity = snapshot.get('velocity') or {}
        sats = snapshot.get('satellites') or {}
        slave = snapshot.get('slave') or {}
        slave_position = slave.get('position') or {}
        slave_velocity = slave.get('velocity') or {}
        slave_sats = slave.get('satellites') or {}
        slave_accuracy = slave.get('accuracy') or {}
        return {
            'recorded_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'utc': velocity.get('utc'),
            'date': velocity.get('date'),
            'lat': position.get('lat'),
            'lon': position.get('lon'),
            'alt': position.get('alt'),
            'geoid_sep': position.get('geoid_sep'),
            'quality': position.get('quality'),
            'quality_text': position.get('quality_text'),
            'fix': position.get('fix'),
            'num_sv': position.get('num_sv'),
            'hdop': position.get('hdop'),
            'heading_deg': heading.get('deg'),
            'heading_source': heading.get('source'),
            'ths_deg': ths.get('deg'),
            'ths_status': ths.get('status'),
            'pitch_deg': log.get('pitch_deg'),
            'baseline_length_m': log.get('baseline_length_m'),
            'sol_status': log.get('sol_status'),
            'pos_type': log.get('pos_type'),
            'heading_stddev_deg': log.get('heading_stddev_deg'),
            'pitch_stddev_deg': log.get('pitch_stddev_deg'),
            'heading_mismatch_deg': heading.get('mismatch_deg'),
            'speed_kn': velocity.get('speed_kn'),
            'speed_kmh': velocity.get('speed_kmh'),
            'course_deg': velocity.get('course_deg'),
            'vel_n_mps': velocity.get('vel_n_mps'),
            'vel_e_mps': velocity.get('vel_e_mps'),
            'vel_u_mps': velocity.get('vel_u_mps'),
            'sats_tracked': sats.get('tracked'),
            'sats_used': sats.get('used'),
            'max_cn0': sats.get('max_cn0'),
            'slave_lat': slave_position.get('lat'),
            'slave_lon': slave_position.get('lon'),
            'slave_alt': slave_position.get('alt'),
            'slave_fix': slave_position.get('quality_text'),
            'slave_sats_used': slave_sats.get('used'),
            'slave_speed_kn': slave_velocity.get('speed_kn'),
            'slave_course_deg': slave_velocity.get('course_deg'),
            'slave_std_lat': slave_accuracy.get('std_lat'),
            'slave_std_lon': slave_accuracy.get('std_lon'),
            'slave_std_alt': slave_accuracy.get('std_alt'),
        }
