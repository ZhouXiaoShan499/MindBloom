"""
MindBloom 桌面仿真器 - 无硬件也能调试核心逻辑
=============================================
在 PC 上用图形界面模拟 MindBloom 台灯的全部行为。
不需要任何硬件，纯软件运行。

使用方法：
    cd d:\Mindbloom12d
    python simulator/mindbloom_simulator.py

操作说明：
    [空格]      切换 人在/离开
    [↑/↓]      调节活动强度
    [R]        一键重置
    [B]        进入/退出 呼吸光
    [1/2/3]    切换灵敏度档位 (快/中/关闭久坐)
"""

import sys
import os
import tkinter as tk

# 将项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from firmware.src.policy_engine import PolicyEngine

# 状态中文名
STATE_CN = {
    "IDLE_OFF": "待机熄灯",
    "BASE_STUDY": "常态学习",
    "LOW_ACTIVITY_NUDGE": "久坐提示",
    "BREATHE_MANUAL": "放松呼吸",
    "LEAVE_DIM": "离开缓灭",
}

# 事件中文名
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


class MindBloomSimulator:
    """图形化 MindBloom 仿真器"""

    def __init__(self):
        self.engine = PolicyEngine()

        # 用户控制
        self.presence = 0       # 0=无人, 1=有人
        self.motion_level = 0.0 # 0~1

        # Tkinter 窗口
        self.root = tk.Tk()
        self.root.title("MindBloom 台灯仿真器")
        self.root.geometry("750x620")
        self.root.resizable(False, False)

        # 设置样式
        self.bg_color = "#1a1a2e"
        self.root.configure(bg=self.bg_color)

        self._build_ui()
        self._bind_keys()

        # 主循环定时器
        self._tick()

    # ---------- UI 构建 ----------

    def _build_ui(self):
        # === 主框架 ===
        main_frame = tk.Frame(self.root, bg=self.bg_color)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        # === 顶部标题 ===
        title = tk.Label(
            main_frame, text="MindBloom 台灯仿真器",
            font=("微软雅黑", 16, "bold"),
            fg="#e0d5c1", bg=self.bg_color
        )
        title.pack(pady=(0, 10))

        # === 中间区域：灯 + 控制面板 ===
        center_frame = tk.Frame(main_frame, bg=self.bg_color)
        center_frame.pack(fill=tk.BOTH, expand=True)

        # ---- 左侧：台灯可视化 ----
        lamp_frame = tk.Frame(center_frame, bg=self.bg_color, width=280)
        lamp_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lamp_frame.pack_propagate(False)

        tk.Label(
            lamp_frame, text="台灯效果",
            font=("微软雅黑", 10), fg="#aaa", bg=self.bg_color
        ).pack(pady=(0, 5))

        # 灯罩画布
        self.canvas = tk.Canvas(
            lamp_frame, width=220, height=220,
            bg="#0d0d1a", highlightthickness=0
        )
        self.canvas.pack(pady=5)

        # 灯罩椭圆
        self.lamp_shade = self.canvas.create_oval(
            30, 30, 190, 170,
            fill="#333355", outline="#555577", width=2
        )
        # 发光圆 (亮度将动态变化)
        self.light_glow = self.canvas.create_oval(
            50, 50, 170, 150,
            fill="#ffffcc", outline="", stipple="gray50"
        )
        # 底座
        self.canvas.create_rectangle(
            60, 180, 160, 200,
            fill="#444466", outline="#555577", width=1
        )
        # "MindBloom" 文字
        self.canvas.create_text(
            110, 205, text="MindBloom",
            fill="#8888aa", font=("Arial", 8)
        )

        # 状态和亮度标签（灯下方）
        info_frame = tk.Frame(lamp_frame, bg=self.bg_color)
        info_frame.pack(pady=5)

        tk.Label(info_frame, text="状态:", fg="#aaa", bg=self.bg_color,
                 font=("微软雅黑", 9)).grid(row=0, column=0, sticky="w")
        self.state_label = tk.Label(
            info_frame, text="待机熄灯", fg="#4fc3f7", bg=self.bg_color,
            font=("微软雅黑", 10, "bold"), width=12
        )
        self.state_label.grid(row=0, column=1, sticky="w")

        tk.Label(info_frame, text="亮度:", fg="#aaa", bg=self.bg_color,
                 font=("微软雅黑", 9)).grid(row=1, column=0, sticky="w")
        self.brightness_label = tk.Label(
            info_frame, text="0%", fg="#ffd54f", bg=self.bg_color,
            font=("微软雅黑", 10, "bold"), width=12
        )
        self.brightness_label.grid(row=1, column=1, sticky="w")

        # ---- 右侧：控制面板 ----
        control_frame = tk.Frame(center_frame, bg="#222244", padx=15, pady=12,
                                 relief=tk.RIDGE, bd=2)
        control_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        tk.Label(
            control_frame, text="控制面板",
            font=("微软雅黑", 11, "bold"), fg="#e0d5c1",
            bg="#222244"
        ).pack(anchor="w", pady=(0, 10))

        # -- 人在/离开 --
        self.presence_btn = tk.Button(
            control_frame, text="人在", width=14,
            bg="#2e7d32", fg="white", font=("微软雅黑", 10),
            command=self._toggle_presence
        )
        self.presence_btn.pack(pady=3, anchor="w")

        # -- 活动强度滑块 --
        tk.Label(control_frame, text="活动强度", fg="#ccc",
                 bg="#222244", font=("微软雅黑", 9)).pack(anchor="w", pady=(8, 0))
        self.motion_slider = tk.Scale(
            control_frame, from_=0, to=100, orient=tk.HORIZONTAL,
            length=180, bg="#222244", fg="#ccc",
            troughcolor="#333355", highlightthickness=0,
            command=self._on_motion_change
        )
        self.motion_slider.set(20)
        self.motion_slider.pack(anchor="w", pady=(0, 5))
        tk.Label(control_frame, text="< 安静          活跃 >", fg="#888",
                 bg="#222244", font=("微软雅黑", 8)).pack(anchor="w")

        # -- 操作按钮 --
        btn_frame = tk.Frame(control_frame, bg="#222244")
        btn_frame.pack(pady=(12, 0), anchor="w")

        self.reset_btn = tk.Button(
            btn_frame, text="重置 [R]", width=12,
            bg="#e65100", fg="white", font=("微软雅黑", 9),
            command=self._do_reset
        )
        self.reset_btn.grid(row=0, column=0, padx=2, pady=2)

        self.breath_btn = tk.Button(
            btn_frame, text="呼吸光 [B]", width=12,
            bg="#4a148c", fg="white", font=("微软雅黑", 9),
            command=self._toggle_breath
        )
        self.breath_btn.grid(row=0, column=1, padx=2, pady=2)

        # -- 灵敏度档位 --
        tk.Label(control_frame, text="灵敏度档位", fg="#ccc",
                 bg="#222244", font=("微软雅黑", 9)).pack(anchor="w", pady=(10, 2))
        sens_frame = tk.Frame(control_frame, bg="#222244")
        sens_frame.pack(anchor="w")

        self.sens_var = tk.StringVar(value="中")
        for i, (text, cfg) in enumerate([
            ("快 [1]", {"nudge_window_sec": 15*60, "nudge_dim_rate": 0.0001}),
            ("中 [2]", {"nudge_window_sec": 30*60, "nudge_dim_rate": 0.000033}),
            ("关 [3]", {"nudge_window_sec": 999999, "nudge_dim_rate": 0}),
        ]):
            btn = tk.Radiobutton(
                sens_frame, text=text, variable=self.sens_var,
                value=text[0], bg="#222244", fg="#ccc",
                selectcolor="#444466", font=("微软雅黑", 9),
                command=lambda c=cfg: self._set_sensitivity(c)
            )
            btn.grid(row=0, column=i, padx=2)

        # -- 事件日志 --
        tk.Label(control_frame, text="事件日志", fg="#ccc",
                 bg="#222244", font=("微软雅黑", 9)).pack(anchor="w", pady=(12, 2))
        self.log_text = tk.Text(
            control_frame, height=5, width=28,
            bg="#1a1a2e", fg="#ccc", font=("Consolas", 8),
            relief=tk.FLAT, state=tk.DISABLED
        )
        self.log_text.pack(anchor="w", fill=tk.X)

        # -- 快捷键提示 --
        tk.Label(control_frame,
                 text="[空格] 切换人在/离开\n"
                      "[上/下] 调活动强度\n"
                      "[R]重置 [B]呼吸 [1/2/3]档位",
                 fg="#666", bg="#222244",
                 font=("微软雅黑", 8), justify=tk.LEFT
        ).pack(anchor="w", pady=(8, 0))

        # === 底部：状态机状态 ===
        status_frame = tk.Frame(main_frame, bg=self.bg_color)
        status_frame.pack(fill=tk.X, pady=(10, 0))

        tk.Label(status_frame, text="内部状态机:", fg="#888",
                 bg=self.bg_color, font=("微软雅黑", 8)).pack(side=tk.LEFT)

        self.detail_label = tk.Label(
            status_frame,
            text="低活动累计: 0s  |  久坐暗量: 0.0  |  离开计时: 0s",
            fg="#aaa", bg=self.bg_color, font=("Consolas", 8)
        )
        self.detail_label.pack(side=tk.LEFT, padx=(10, 0))

    # ---------- 键盘绑定 ----------

    def _bind_keys(self):
        self.root.bind("<space>", lambda e: self._toggle_presence())
        self.root.bind("<Up>", lambda e: self._adjust_motion(5))
        self.root.bind("<Down>", lambda e: self._adjust_motion(-5))
        self.root.bind("r", lambda e: self._do_reset())
        self.root.bind("R", lambda e: self._do_reset())
        self.root.bind("b", lambda e: self._toggle_breath())
        self.root.bind("B", lambda e: self._toggle_breath())
        self.root.bind("1", lambda e: self._set_sensitivity(
            {"nudge_window_sec": 15*60, "nudge_dim_rate": 0.0001}))
        self.root.bind("2", lambda e: self._set_sensitivity(
            {"nudge_window_sec": 30*60, "nudge_dim_rate": 0.000033}))
        self.root.bind("3", lambda e: self._set_sensitivity(
            {"nudge_window_sec": 999999, "nudge_dim_rate": 0}))

    # ---------- 控制方法 ----------

    def _toggle_presence(self):
        self.presence = 1 if self.presence == 0 else 0
        if self.presence:
            self.presence_btn.config(text="人在", bg="#2e7d32")
            self._log("人来")
        else:
            self.presence_btn.config(text="离开", bg="#b71c1c")
            self._log("离开")

    def _adjust_motion(self, delta):
        val = self.motion_slider.get() + delta
        val = max(0, min(100, val))
        self.motion_slider.set(val)
        self.motion_level = val / 100.0

    def _on_motion_change(self, val):
        self.motion_level = int(val) / 100.0

    def _do_reset(self):
        self.engine.reset()
        self._log("一键重置")

    def _toggle_breath(self):
        if self.engine.is_breathing:
            self.engine.exit_breathing()
            self.breath_btn.config(text="呼吸光 [B]", bg="#4a148c")
            self._log("退出呼吸")
        else:
            if self.engine.enter_breathing():
                self.breath_btn.config(text="呼吸中...", bg="#7b1fa2")
                self._log("进入呼吸")
            else:
                self._log("无法进入呼吸(无人)")

    def _set_sensitivity(self, cfg):
        self.engine.cfg.update(cfg)
        label = {15*60: "快", 30*60: "中", 999999: "关"}
        lbl = label.get(cfg.get("nudge_window_sec"), "?")
        self._log(f"灵敏度: {lbl}")

    # ---------- 日志 ----------

    def _log(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"> {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ---------- 主循环 ----------

    def _tick(self):
        """每 50ms 更新一次状态"""
        dt = 0.05

        # 更新引擎
        self.engine.update(self.presence, self.motion_level, dt)

        # 获取当前亮度
        brightness = self.engine.current_brightness

        # 更新灯可视化
        self._update_lamp(brightness)

        # 更新标签
        self.state_label.config(text=STATE_CN.get(self.engine.state, self.engine.state))
        self.brightness_label.config(text=f"{int(brightness * 100)}%")

        # 检查是否有新事件
        if self.engine.last_event:
            cn = EVENT_CN.get(self.engine.last_event, self.engine.last_event)
            self._log(cn)
            self.engine.last_event = ""

        # 更新详情
        self.detail_label.config(
            text=(f"低活动累计: {self.engine.low_activity_time:.0f}s  | "
                  f"久坐暗量: {self.engine.nudge_dim_amount:.3f}  | "
                  f"离开计时: {self.engine.leave_timer:.0f}s")
        )

        # 继续循环
        self.root.after(50, self._tick)

    def _update_lamp(self, brightness):
        """更新画布上的灯效果"""
        bright_val = max(0.1, brightness)

        # 灯罩颜色随亮度变化
        r = int(40 + 80 * bright_val)
        g = int(40 + 70 * bright_val)
        b = int(60 + 100 * bright_val)
        shade_color = f"#{r:02x}{g:02x}{b:02x}"
        self.canvas.itemconfig(self.lamp_shade, fill=shade_color)

        # 发光圆 (暖白到冷白)
        if brightness < 0.05:
            glow_color = "#111122"
        else:
            warm = int(200 * brightness)
            cool = int(180 * brightness)
            glow_color = f"#ff{warm:02x}{cool:02x}"
        self.canvas.itemconfig(self.light_glow, fill=glow_color)

        # 呼吸光时边框变紫色
        if self.engine.is_breathing:
            self.canvas.itemconfig(self.lamp_shade, outline="#ff66ff", width=2)
        else:
            self.canvas.itemconfig(self.lamp_shade, outline="#555577", width=2)

    # ---------- 启动 ----------

    def run(self):
        self.root.mainloop()


# ====================== 程序入口 ======================
if __name__ == "__main__":
    print("=" * 50)
    print("MindBloom 桌面仿真器")
    print("=" * 50)
    print("操作说明：")
    print("  [空格]  切换 人在/离开")
    print("  [上/下] 调节活动强度")
    print("  [R]     一键重置")
    print("  [B]     进入/退出 呼吸光")
    print("  [1/2/3] 切换灵敏度档位 (快/中/关)")

    sim = MindBloomSimulator()
    sim.run()
