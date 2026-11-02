"""仅生成需规插图，不属于系统实现；PlantUML文本从当前Markdown同步提取。"""
from pathlib import Path
import re
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCE = ROOT / "03_需求规格说明书.md"
doc = SOURCE.read_text(encoding="utf-8")
version = re.search(r"版本：([^；]+)", doc).group(1)
FONT = "C:/Windows/Fonts/msyh.ttc"
im = Image.new("RGB", (1800, 1470), "white")
d = ImageDraw.Draw(im)
fonts = {n: ImageFont.truetype(FONT, n) for n in (23, 25, 28, 34, 40)}

def centered(x, y, text, size=28, fill="#1e293b"):
    box = d.multiline_textbbox((0, 0), text, font=fonts[size], align="center", spacing=9)
    d.multiline_text((x-(box[2]-box[0])/2, y-(box[3]-box[1])/2), text,
                     font=fonts[size], fill=fill, align="center", spacing=9)

def actor(x, y, label):
    color = "#334155"
    d.ellipse((x-21, y-68, x+21, y-26), outline=color, width=4)
    d.line([(x, y-26), (x, y+31)], fill=color, width=4)
    d.line([(x-38, y-8), (x+38, y-8)], fill=color, width=4)
    d.line([(x, y+31), (x-32, y+72)], fill=color, width=4)
    d.line([(x, y+31), (x+32, y+72)], fill=color, width=4)
    centered(x, y+108, label, 28)

centered(900, 37, "VetRules 三角色用例图", 40)
d.rectangle((255, 95, 1545, 1320), outline="#64748b", width=3)
centered(900, 122, "VetRules 系统边界", 28)
cases = {
    1: (580, 210, "UC-01 接待建档"),
    2: (580, 370, "UC-02 查询与记录诊疗"),
    3: (580, 530, "UC-03 追加处方与更正"),
    4: (580, 690, "UC-04 登记AI建议处理"),
    5: (580, 850, "UC-05 处理病历告警"),
    8: (580, 1010, "UC-08 导出病历"),
    9: (900, 1200, "UC-09 导出护栏命中\n医生明细 / 管理脱敏汇总"),
    6: (1230, 210, "UC-06 审批并启用\n规则与模板"),
    7: (1230, 410, "UC-07 查看统计\n与脱敏审计"),
    10: (1230, 610, "UC-10 生成控制文件"),
    11: (1230, 810, "UC-11 维护人机分工登记"),
    12: (1230, 1010, "UC-12 队列恢复补发\n内部恢复场景"),
}
for n in (1,):
    x, y, _ = cases[n]
    d.line([(148, 202), (x-228, y)], fill="#64748b", width=3)
for n in (2, 3, 4, 5, 8):
    x, y, _ = cases[n]
    d.line([(148, 682), (x-228, y)], fill="#64748b", width=3)
for n in (6, 7, 10, 11):
    x, y, _ = cases[n]
    d.line([(1652, 602), (x+228, y)], fill="#64748b", width=3)
d.line([(148, 682), (200, 1120), (672, 1200)], fill="#64748b", width=3)
d.line([(1652, 602), (1600, 1120), (1128, 1200)], fill="#64748b", width=3)
for n, (x, y, label) in cases.items():
    color = "#f1f5f9" if n == 12 else "#eff6ff"
    d.ellipse((x-228, y-57, x+228, y+57), fill=color, outline="#475569", width=3)
    centered(x, y-5, label, 25 if n in (9, 12) else 28)
actor(100, 210, "前台账号")
actor(100, 690, "医生账号")
actor(1700, 610, "管理账号")
centered(1230, 1090, "恢复/重试触发，无人工关联线", 23, "#64748b")
centered(900, 1380, "关联线表示参与关系，不表示执行顺序。宠物主人无登录入口。", 25)
centered(900, 1422, f"UC-09导出范围按需规C-05；UC-12为内部恢复场景。{version}", 25)
im.save(HERE / "05_三角色用例图_v0.6.png")
uml = re.search(r"```plantuml\n(.*?)\n```", doc, re.S).group(1)
(HERE / "03_三角色用例图.puml").write_text(uml + "\n", encoding="utf-8")
print("已生成用例图及匹配的PlantUML文本")
