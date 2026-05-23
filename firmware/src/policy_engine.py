"""
MindBloom Policy Engine - AI 环境智能核心逻辑
============================================
完全与硬件无关，可在 PC 上直接运行和测试。
后续移植到 ESP32 C++/TFLite Micro 时，逻辑保持不变。

V2 升级：从固定规则 → 统计学习用户行为模式

新增 AI 能力（Phase 1）：
  1. 自适应安静基线：motion_level 阈值不再是硬编码 0.3，
     而是根据你过去 N 次使用的实际数据动态调整。
  2. 专注时长学习：记录你每次主动结束学习前专注了多久，
     动态调整 nudge_window_sec。
  3. 所有数据仅存本地，不上云。

内部状态：
  IDLE_OFF           - 无人 -> 灯灭/待机
  BASE_STUDY         - 在位且未触发久坐提示
  LOW_ACTIVITY_NUDGE - 长时间低活动累计超过阈值
  BREATHE_MANUAL     - 手动进入呼吸放松模式
  LEAVE_DIM          - 离开后延时缓灭

优先级（从高到低）：
  用户重置/手动退出呼吸 -> 离开熄灯逻辑 -> 呼吸模式内规则 -> 久坐缓暗累计
"""

import math
import json
import os
import time

# ====================== 可配置参数 ======================
CONFIG_DEFAULT = {
    # 久坐提示（初始默认值，AI 会动态调整）
    "nudge_window_sec": 25 * 60,        # 连续低活动多久触发 (初始 25 分钟)
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

    # ---- AI 学习参数 ----
    "learning_enabled": True,           # 是否开启自适应学习（默认开）
    "learning_rate": 0.1,               # 学习速率 (0~1)，越大适应越快
    "profile_save_path": "",            # 用户画像持久化路径（空=不存文件）
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
    "profile_adapted": "AI已适应",
}


class UserProfile:
    """
    用户行为画像 —— 记录并学习用户的个人使用模式。

    所有数据仅存本地，不上云。学习逻辑基于简单统计（指数移动平均），
    不需要神经网络即可运行在 ESP32-S3 上。
    """

    def __init__(self, config=None):
        self.cfg = config or {}

        # ---- 自适应阈值 ----
        # 用户的"安静"基线：motion_level 低于此值视为低活动
        # 初始 0.30，随使用慢慢调整到用户的实际水平
        self.motion_baseline = 0.30

        # 用户的"活跃"阈值：超过此值视为有明显活动
        self.motion_active_threshold = 0.45

        # ---- 专注模式统计 ----
        # 用户典型的专注时长（秒），用于动态调整 nudge_window
        self.typical_focus_duration = 25 * 60  # 初始 25 分钟

        # 记录历史专注时长（最近 N 次）
        self.recent_focus_sessions = []  # list of durations in seconds
        self.max_history = 20            # 最多保留 20 次

        # ---- 使用习惯 ----
        # 用户偏好的色温（暖=0.0 ~ 冷=1.0），通过学习慢慢调整
        self.preferred_color_temp = 0.5

        # 用户通常会在桌前持续多久（分钟），用于学习使用时段
        self.typical_session_length = 60 * 60  # 初始 60 分钟

        # ---- 学习计数 ----
        self.total_sessions = 0          # 总学习次数
        self.adaptation_count = 0        # 自适应调整次数

        # ---- 当前会话状态（用于学习） ----
        self._session_start_time = None
        self._session_focus_start = None
        self._is_in_focus = False
        self._current_focus_duration = 0.0

    # ---------- 核心学习接口 ----------

    def observe_motion(self, motion_level):
        """
        实时观察 motion_level，平滑更新基线。
        会在每次 update 中被调用。
        """
        if not self.cfg.get("learning_enabled", True):
            return

        lr = self.cfg.get("learning_rate", 0.1)

        # 只采集"安静"时段的数据（motion_level < 当前基线 + 0.1）
        # 这样基线会逐渐收敛到用户真正"安静"的水平
        if motion_level < self.motion_baseline + 0.1:
            # 指数移动平均：新基线 = (1-lr)*旧基线 + lr*当前值
            self.motion_baseline = (1 - lr) * self.motion_baseline + lr * motion_level
            # 保底：安静基线不能太低（0.05）也不能太高（0.50）
            self.motion_baseline = max(0.05, min(0.50, self.motion_baseline))

        # 活跃阈值 = 安静基线 + 固定偏移
        self.motion_active_threshold = self.motion_baseline + 0.20
        self.motion_active_threshold = max(0.25, min(0.80, self.motion_active_threshold))

    def observe_focus_start(self):
        """用户开始进入专注状态时调用"""
        self._session_focus_start = time.time()
        self._is_in_focus = True
        self._current_focus_duration = 0.0

    def observe_focus_end(self, was_reset=False):
        """
        用户结束一段专注时调用（例如活动回升、重置）。
        记录这次专注了多久，更新 typical_focus_duration。
        """
        if not self._is_in_focus or self._session_focus_start is None:
            return

        duration = time.time() - self._session_focus_start

        # 只记录有意义的专注（>5 分钟）
        if duration > 5 * 60:
            self.recent_focus_sessions.append(duration)
            if len(self.recent_focus_sessions) > self.max_history:
                self.recent_focus_sessions.pop(0)

            # 更新 typical_focus_duration：取中位数（抗异常值）
            if self.recent_focus_sessions:
                sorted_durations = sorted(self.recent_focus_sessions)
                median = sorted_durations[len(sorted_durations) // 2]
                self.typical_focus_duration = median

                # 计数
                self.adaptation_count += 1

        self._is_in_focus = False
        self._session_focus_start = None

    def observe_session_start(self):
        """用户开始一次学习时调用"""
        self._session_start_time = time.time()
        self.total_sessions += 1

    def observe_session_end(self):
        """用户结束一次学习时调用"""
        if self._session_start_time is None:
            return
        duration = time.time() - self._session_start_time
        if duration > 10 * 60:  # 只记录 >10 分钟的 session
            # 指数移动平均更新 typical_session_length
            lr = self.cfg.get("learning_rate", 0.1)
            self.typical_session_length = (1 - lr) * self.typical_session_length + lr * duration

        self._session_start_time = None

    def get_nudge_window(self):
        """
        返回推荐的低活动触发窗口（秒）。
        基于用户的 typical_focus_duration 做调整。
        """
        # nudge_window 建议设为 typical_focus_duration 的 70%~100%
        # 这样不会在用户通常的专注周期内打断
        window = self.typical_focus_duration * 0.85
        # 保底范围：10 分钟 ~ 60 分钟
        return max(10 * 60, min(60 * 60, window))

    def get_low_activity_threshold(self):
        """返回自适应低活动阈值"""
        return self.motion_baseline

    def get_active_threshold(self):
        """返回自适应活跃阈值"""
        return self.motion_active_threshold

    # ---------- 持久化 ----------

    def to_dict(self):
        """将画像导出为可序列化的字典（用于本地存储）"""
        return {
            "motion_baseline": self.motion_baseline,
            "motion_active_threshold": self.motion_active_threshold,
            "typical_focus_duration": self.typical_focus_duration,
            "recent_focus_sessions": self.recent_focus_sessions,
            "preferred_color_temp": self.preferred_color_temp,
            "typical_session_length": self.typical_session_length,
            "total_sessions": self.total_sessions,
            "adaptation_count": self.adaptation_count,
        }

    def from_dict(self, data):
        """从字典恢复画像"""
        self.motion_baseline = data.get("motion_baseline", 0.30)
        self.motion_active_threshold = data.get("motion_active_threshold", 0.45)
        self.typical_focus_duration = data.get("typical_focus_duration", 25 * 60)
        self.recent_focus_sessions = data.get("recent_focus_sessions", [])
        self.preferred_color_temp = data.get("preferred_color_temp", 0.5)
        self.typical_session_length = data.get("typical_session_length", 60 * 60)
        self.total_sessions = data.get("total_sessions", 0)
        self.adaptation_count = data.get("adaptation_count", 0)

    def save(self, filepath):
        """保存画像到本地文件"""
        try:
            with open(filepath, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
            return True
        except Exception:
            return False

    def load(self, filepath):
        """从本地文件加载画像"""
        try:
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    data = json.load(f)
                self.from_dict(data)
                return True
        except Exception:
            pass
        return False

    def summary(self):
        """返回画像摘要（用于调试和 UI 展示）"""
        return (
            f"安静基线:{self.motion_baseline:.2f} | "
            f"活跃阈值:{self.motion_active_threshold:.2f} | "
            f"典型专注:{self.typical_focus_duration/60:.0f}min | "
            f"总学习:{self.total_sessions}次 | "
            f"已适应:{self.adaptation_count}次"
        )


class PolicyEngine:
    """
    策略引擎 - 接收雷达特征事件，输出目标亮度/色温/模式。

    V2 新增：
      - 自适应阈值（UserProfile）
      - 动态调整 nudge_window
      - 学习用户行为模式

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

        # 活动检测（旧版硬编码阈值保留作为 fallback）
        self._last_motion_level = 1.0
        self._motion_bump_threshold = 0.15  # 活动回升检测阈值（固定）
        self._hard_low_activity_threshold = 0.3  # 旧版硬编码阈值

        # ---- 新增：用户行为画像 ----
        self.profile = UserProfile(self.cfg)

        # 加载历史画像
        if self.cfg.get("profile_save_path"):
            self.profile.load(self.cfg["profile_save_path"])

        # 应用学习到的 nudge_window
        learned_window = self.profile.get_nudge_window()
        self.cfg["nudge_window_sec"] = learned_window

        # 打标
        self.last_event = ""

    # ---------- 属性 ----------

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

    @property
    def adaptive_threshold(self):
        """当前正在使用的低活动阈值"""
        if self.cfg.get("learning_enabled", True):
            return self.profile.get_low_activity_threshold()
        return self._hard_low_activity_threshold

    @property
    def adaptive_active_threshold(self):
        """当前正在使用的活跃阈值"""
        if self.cfg.get("learning_enabled", True):
            return self.profile.get_active_threshold()
        return 0.4  # 旧版硬编码

    # ---------- 重置 ----------

    def reset(self):
        """一键重置：恢复常态，清零累计"""
        self.low_activity_time = 0.0
        self.nudge_dim_amount = 0.0

        # 记录专注结束（重置也认为是主动结束）
        self.profile.observe_focus_end(was_reset=True)

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

        # ---- 学习层：实时观察 motion_level ----
        if presence == 1:
            self.profile.observe_motion(motion_level)

        # ---- 获取自适应阈值 ----
        low_th = self.adaptive_threshold
        active_th = self.adaptive_active_threshold

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
                # 结束学习会话
                self.profile.observe_session_end()
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
                # 开始学习会话
                self.profile.observe_session_start()

            elif self.state == STATE_LEAVE_DIM:
                # 离开中又回来了 -> 快速恢复
                self._transition_to(STATE_BASE_STUDY)
                self.target_brightness = 1.0
                self.last_event = "return"

            elif self.state == STATE_BASE_STUDY:
                # 更新低活动累计（使用自适应阈值）
                is_low_activity = motion_level < low_th
                if is_low_activity:
                    self.low_activity_time += dt
                else:
                    # 有活动时：如果之前低活动累计了一段时间，视为专注结束
                    if self.low_activity_time > 10:
                        self.last_event = "activity_recovery"
                        self.profile.observe_focus_end()
                    self.low_activity_time = 0.0
                    # 开始新的专注
                    self.profile.observe_focus_start()

                # 检查是否触发久坐提示（使用动态窗口）
                if self.low_activity_time >= self.cfg["nudge_window_sec"]:
                    self._transition_to(STATE_LOW_ACTIVITY_NUDGE)
                    self.last_event = "nudge_start"

            elif self.state == STATE_LOW_ACTIVITY_NUDGE:
                if motion_bump or motion_level >= active_th:
                    self.last_event = "activity_recovery"
                    self.profile.observe_focus_end()
                    self._transition_to(STATE_BASE_STUDY)
                    self.low_activity_time = 0.0
                    self.nudge_dim_amount = 0.0
                    self.target_brightness = 1.0
                    # 开始新的专注
                    self.profile.observe_focus_start()
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

        # ---- 定期保存画像（每 100 次更新存一次） ----
        if self.cfg.get("profile_save_path") and random_check():
            self.profile.save(self.cfg["profile_save_path"])

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


# ---------- 辅助函数 ----------

_counter = 0


def random_check(mod=100):
    """每约 mod 次返回 True"""
    global _counter
    _counter += 1
    if _counter >= mod:
        _counter = 0
        return True
    return False


# ====================== 快速自测 ======================
if __name__ == "__main__":
    print("=" * 60)
    print("MindBloom Policy Engine V2 - 自测")
    print("=" * 60)

    engine = PolicyEngine()
    engine.cfg["nudge_window_sec"] = 4.0   # 4 秒触发，方便测试
    engine.cfg["nudge_dim_rate"] = 0.01    # 暗得快一点
    engine.cfg["learning_enabled"] = True

    print(f"\n初始画像: {engine.profile.summary()}")

    print("\n>>> 模拟：人来 -> 久坐触发 -> 活动回升")
    for sec in range(25):
        if sec < 2:
            p, m = 1, 0.5        # 有人，活跃
        elif sec < 10:
            p, m = 1, 0.1        # 有人，安静（触发久坐）
        else:
            p, m = 1, 0.6        # 活动回升
        b = engine.update(p, m, 1.0)
        state = engine.state_cn
        event = engine.last_event_cn
        if event:
            print(f"  t={sec}s  state={state:8s}  brightness={b:.3f}  [{event}]")
            engine.last_event = ""
        elif sec % 3 == 0:
            print(f"  t={sec}s  state={state:8s}  brightness={b:.3f}")

    print(f"\n画像（一轮后）: {engine.profile.summary()}")

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

    print(f"\n最终画像: {engine.profile.summary()}")
    print("\n[OK] 自测完成")
