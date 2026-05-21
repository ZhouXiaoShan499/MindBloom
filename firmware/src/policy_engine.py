"""
MindBloom Policy Engine - 状态机核心逻辑
========================================
完全与硬件无关，可在 PC 上直接运行和测试。
后续移植到 ESP32 C++ 时，逻辑保持不变。

内部状态：
  IDLE_OFF          - 无人 -> 灯灭/待机
  BASE_STUDY        - 在位且未触发久坐提示
  LOW_ACTIVITY_NUDGE - 长时间低活动累计超过阈值
  BREATHE_MANUAL    - 手动进入呼吸放松模式
  LEAVE_DIM         - 离开后延时缓灭

优先级（从高到低）：
  用户重置/手动退出呼吸 -> 离开熄灯逻辑 -> 呼吸模式内规则 -> 久坐缓暗累计
"""

import math

# ====================== 可配置参数 ======================
CONFIG_DEFAULT = {
    # 久坐提示
    "nudge_window_sec": 25 * 60,        # 连续低活动多久触发 (25~35 分钟)
    "nudge_dim_rate": 0.000033,         # 每 15 分钟 -3%，即每秒下降 0.03/(15*60)
    "nudge_brightness_floor": 0.60,     # 亮度下限 (60%)
    "nudge_max_dim_total": 0.30,        # 最多暗多少 (从 1.0 到 0.70)

    # 呼吸光
    "breath_period_sec": 11.0,          # 周期 10~12 秒
    "breath_amplitude": 0.08,           # +-8%

    # 离开缓灭
    "leave_delay_sec": 30,              # 离开后等待多久开始缓灭
    "leave_fade_sec": 8,                # 缓灭持续时间

    # 人来亮
    "arrive_fade_sec": 2.5,             # 2~3 秒渐亮

    # 活动回升
    "recovery_fade_sec": 5.0,           # 数秒内平滑恢复

    # 呼吸光进入退出
    "breath_enter_hold_sec": 2.0,       # 长按 2 秒进入
}


# ====================== 状态常量 ======================
STATE_IDLE_OFF = "IDLE_OFF"
STATE_BASE_STUDY = "BASE_STUDY"
STATE_LOW_ACTIVITY_NUDGE = "LOW_ACTIVITY_NUDGE"
STATE_BREATHE_MANUAL = "BREATHE_MANUAL"
STATE_LEAVE_DIM = "LEAVE_DIM"


# 状态中文标签
STATE_CN = {
    STATE_IDLE_OFF: "待机熄灯",
    STATE_BASE_STUDY: "常态学习",
    STATE_LOW_ACTIVITY_NUDGE: "久坐提示",
    STATE_BREATHE_MANUAL: "放松呼吸",
    STATE_LEAVE_DIM: "离开缓灭",
}

# 事件中文标签
EVENT_CN = {
    "arrive": "人来亮",
    "return": "中途返回",
    "nudge_start": "久坐触发",
    "activity_recovery": "活动恢复",
    "breath_enter": "进入呼吸",
    "breath_exit": "退出呼吸",
    "reset": "一键重置",
    "leave_off": "熄灯",
}


class PolicyEngine:
    """
    策略引擎 - 接收雷达特征事件，输出目标亮度/色温/模式。

    使用方法：
        engine = PolicyEngine()
        engine.update(presence=1, motion_level=0.3, dt=0.1)
        brightness = engine.current_brightness
        is_breathing = engine.is_breathing
    """

    def __init__(self, config=None):
        self.cfg = {**CONFIG_DEFAULT, **(config or {})}

        # 状态
        self.state = STATE_IDLE_OFF
        self.prev_state = STATE_IDLE_OFF

        # 亮度 (归一化 0.0 ~ 1.0)
        self.target_brightness = 0.0
        self.current_brightness = 0.0

        # 计时器
        self.low_activity_time = 0.0       # 当前连续低活动累计 (秒)
        self.nudge_dim_amount = 0.0        # 久坐已经暗了多少 (归一化)
        self.leave_timer = 0.0             # 离开等待计时
        self.state_time = 0.0              # 当前状态持续了多久

        # 呼吸光状态
        self._is_breathing = False

        # 活动检测
        self._last_motion_level = 1.0
        self._motion_bump_threshold = 0.15  # 活动回升检测阈值

        # 打标
        self.last_event = ""

    @property
    def is_breathing(self):
        return self._is_breathing

    @property
    def brightness(self):
        return self.current_brightness

    @property
    def state_cn(self):
        return STATE_CN.get(self.state, self.state)

    @property
    def last_event_cn(self):
        return EVENT_CN.get(self.last_event, self.last_event)

    def reset(self):
        """一键重置：恢复常态，清零累计"""
        self.low_activity_time = 0.0
        self.nudge_dim_amount = 0.0
        self._is_breathing = False
        self.target_brightness = 1.0
        self.last_event = "reset"
        if self.state in (STATE_LOW_ACTIVITY_NUDGE, STATE_BREATHE_MANUAL, STATE_LEAVE_DIM):
            self._transition_to(STATE_BASE_STUDY)
        return self.current_brightness

    # ---------- 核心更新 ----------

    def update(self, presence, motion_level, dt=0.1):
        """
        主更新函数，每次循环调用。

        参数：
            presence:  0=无人, 1=有人
            motion_level: 0.0~1.0, 0=完全静止, 1=剧烈活动
                          (由雷达能量/事件率映射而来)
            dt: 距离上次更新的时间间隔 (秒)
        """
        self.prev_state = self.state
        self.state_time += dt

        # 检测活动回升事件 (motion bump)
        motion_bump = (motion_level - self._last_motion_level) > self._motion_bump_threshold
        self._last_motion_level = motion_level

        # 根据当前状态执行逻辑
        if presence == 0:
            # === 无人 ===
            if self.state == STATE_IDLE_OFF:
                pass
            elif self.state == STATE_LEAVE_DIM:
                self._update_leave_dim(dt)
            else:
                # 任何有人状态 -> 进入离开缓灭
                self._transition_to(STATE_LEAVE_DIM)
                self.leave_timer = 0.0

        else:
            # === 有人 ===
            if self.state == STATE_IDLE_OFF:
                # 人来亮
                self._transition_to(STATE_BASE_STUDY)
                self.target_brightness = 1.0
                self.last_event = "arrive"

            elif self.state == STATE_LEAVE_DIM:
                # 离开中又回来了 -> 快速恢复
                self._transition_to(STATE_BASE_STUDY)
                self.target_brightness = 1.0
                self.last_event = "return"

            elif self.state == STATE_BASE_STUDY:
                # 更新低活动累计
                is_low_activity = motion_level < 0.3
                if is_low_activity:
                    self.low_activity_time += dt
                else:
                    if self.low_activity_time > 10:
                        self.last_event = "activity_recovery"
                    self.low_activity_time = 0.0

                # 检查是否触发久坐提示
                if self.low_activity_time >= self.cfg["nudge_window_sec"]:
                    self._transition_to(STATE_LOW_ACTIVITY_NUDGE)
                    self.last_event = "nudge_start"

            elif self.state == STATE_LOW_ACTIVITY_NUDGE:
                if motion_bump or motion_level >= 0.4:
                    self.last_event = "activity_recovery"
                    self._transition_to(STATE_BASE_STUDY)
                    self.low_activity_time = 0.0
                    self.nudge_dim_amount = 0.0
                    self.target_brightness = 1.0
                else:
                    # 继续变暗
                    self.nudge_dim_amount += self.cfg["nudge_dim_rate"] * dt
                    max_dim = self.cfg["nudge_max_dim_total"]
                    self.nudge_dim_amount = min(self.nudge_dim_amount, max_dim)
                    self.target_brightness = 1.0 - self.nudge_dim_amount
                    self.target_brightness = max(self.target_brightness, self.cfg["nudge_brightness_floor"])

            elif self.state == STATE_BREATHE_MANUAL:
                pass  # 呼吸光由外部控制

        # 呼吸光内部更新
        if self._is_breathing and self.state == STATE_BREATHE_MANUAL:
            self._update_breath()
        elif self._is_breathing and self.state != STATE_BREATHE_MANUAL:
            self._is_breathing = False

        # 平滑逼近目标亮度
        self._smooth_brightness(dt)

        return self.current_brightness

    # ---------- 呼吸光控制 ----------

    def enter_breathing(self):
        """长按约 2 秒后调用"""
        if self.state != STATE_IDLE_OFF and self.state != STATE_LEAVE_DIM:
            self._transition_to(STATE_BREATHE_MANUAL)
            self._is_breathing = True
            self.last_event = "breath_enter"
            return True
        return False

    def exit_breathing(self):
        """短按/重置退出呼吸光"""
        if self.state == STATE_BREATHE_MANUAL:
            self._is_breathing = False
            self.target_brightness = 1.0
            self._transition_to(STATE_BASE_STUDY)
            self.last_event = "breath_exit"
            return True
        return False

    # ---------- 内部方法 ----------

    def _transition_to(self, new_state):
        self.state = new_state
        self.state_time = 0.0

    def _update_breath(self):
        """呼吸波形：正弦波 (使用状态时间)"""
        elapsed = self.state_time
        phase = (elapsed % self.cfg["breath_period_sec"]) / self.cfg["breath_period_sec"]
        sine_val = math.sin(phase * 2 * math.pi)
        base = 1.0
        amp = self.cfg["breath_amplitude"]
        self.target_brightness = base + sine_val * amp
        self.target_brightness = max(0.0, min(1.0, self.target_brightness))

    def _update_leave_dim(self, dt):
        self.leave_timer += dt
        if self.leave_timer >= self.cfg["leave_delay_sec"]:
            fade_progress = (self.leave_timer - self.cfg["leave_delay_sec"]) / self.cfg["leave_fade_sec"]
            fade_progress = min(fade_progress, 1.0)
            self.target_brightness = 1.0 - fade_progress
            if fade_progress >= 1.0:
                self._transition_to(STATE_IDLE_OFF)
                self.target_brightness = 0.0
                self.last_event = "leave_off"
        else:
            self.target_brightness = 1.0

    def _smooth_brightness(self, dt):
        """平滑过渡到目标亮度"""
        diff = self.target_brightness - self.current_brightness
        max_change_per_sec = 0.3
        max_step = max_change_per_sec * dt
        if abs(diff) > max_step:
            diff = max_step if diff > 0 else -max_step
        self.current_brightness += diff
        self.current_brightness = max(0.0, min(1.0, self.current_brightness))


# ====================== 快速自测 ======================
if __name__ == "__main__":
    print("=" * 50)
    print("MindBloom Policy Engine - 自测")
    print("=" * 50)

    engine = PolicyEngine()
    engine.cfg["nudge_window_sec"] = 3.0   # 3 秒触发，方便测试
    engine.cfg["nudge_dim_rate"] = 0.01    # 暗得快一点

    print("\n>>> 模拟：人来 -> 久坐触发")
    for sec in range(20):
        if sec < 3:
            p, m = 1, 0.5
        elif sec < 6:
            p, m = 1, 0.1
        else:
            p, m = 1, 0.1
        b = engine.update(p, m, 1.0)
        state = engine.state_cn
        event = engine.last_event_cn
        if event:
            print(f"  t={sec}s  state={state:8s}  brightness={b:.3f}  [{event}]")
            engine.last_event = ""
        elif sec % 3 == 0:
            print(f"  t={sec}s  state={state:8s}  brightness={b:.3f}")

    print("\n>>> 活动回升")
    engine.update(1, 0.7, 1.0)
    print(f"  t=21s  brightness={engine.current_brightness:.3f}  [{engine.last_event_cn}]")
    engine.last_event = ""
    for sec in range(5):
        engine.update(1, 0.3, 1.0)
        print(f"  t={22+sec}s  state={engine.state_cn:8s}  brightness={engine.current_brightness:.3f}")

    print("\n>>> 呼吸光")
    engine.enter_breathing()
    for sec in range(15):
        engine.update(1, 0.2, 0.5)
        if sec % 2 == 0:
            print(f"  t={sec}s  brightness={engine.current_brightness:.4f}  state={engine.state_cn}")

    print("\n>>> 重置")
    engine.reset()
    print(f"  brightness={engine.current_brightness:.3f}  state={engine.state_cn}")

    print("\n>>> 离开缓灭")
    for sec in range(50):
        engine.update(0, 0.0, 1.0)
        if sec % 5 == 0:
            print(f"  t={sec}s  state={engine.state_cn:8s}  brightness={engine.current_brightness:.3f}")

    print("\n[OK] 自测完成")
