"""需求文档插图辅助工具；读取03的PlantUML文本，不是VetRules业务实现。"""
from pathlib import Path
import math
import re
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
doc = (ROOT / '03_需求规格说明书.md').read_text(encoding='utf-8')
blocks = re.findall(r'```plantuml\n(.*?)\n```', doc, re.S)
assert len(blocks) == 2
for name, block in zip(('04_三角色目标总览.puml', '05_共用与条件关系.puml'), blocks):
    (HERE / name).write_text(block + '\n', encoding='utf-8')
font_path = 'C:/Windows/Fonts/msyh.ttc'
fonts = {n: ImageFont.truetype(font_path, n) for n in (25, 28, 31, 34, 38, 40, 46)}
ink, line, blue = '#172b4d', '#526579', '#f0f6fc'

def center(d, x, y, text, size=34, fill=ink):
    box = d.multiline_textbbox((0, 0), text, font=fonts[size], align='center', spacing=8)
    d.multiline_text((x-(box[2]-box[0])/2, y-(box[3]-box[1])/2), text,
                     font=fonts[size], fill=fill, align='center', spacing=8)

def actor(d, x, y, label):
    d.ellipse((x-23, y-70, x+23, y-24), outline=ink, width=4)
    d.line([(x, y-24), (x, y+36)], fill=ink, width=4)
    d.line([(x-38, y), (x+38, y)], fill=ink, width=4)
    d.line([(x, y+36), (x-35, y+78)], fill=ink, width=4)
    d.line([(x, y+36), (x+35, y+78)], fill=ink, width=4)
    center(d, x, y+120, label, 31)

def oval(d, x, y, label, rx=465, ry=51, size=34):
    d.ellipse((x-rx, y-ry, x+rx, y+ry), fill=blue, outline=line, width=3)
    center(d, x, y-3, label, size)

cases = {alias: label.replace('\\n', '\n') for label, alias in
         re.findall(r'usecase "([^"]+)" as (\w+)', blocks[0])}
edges = re.findall(r'^(Front|Doctor|Manager) -- (\w+)$', blocks[0], re.M)
assert len(cases) == len(edges) == 20
im = Image.new('RGB', (1800, 3010), 'white')
d = ImageDraw.Draw(im)
center(d, 900, 44, 'VetRules 三角色目标总览 · v0.8', 46)
d.rectangle((330, 105, 1720, 2890), outline=line, width=4)
center(d, 1030, 141, 'VetRules 系统边界', 34)
y = 250
for role, label in [('Front', '前台账号'), ('Doctor', '医生账号'), ('Manager', '管理账号')]:
    aliases = [a for r, a in edges if r == role]
    centers = [y+i*127 for i in range(len(aliases))]
    middle = (centers[0]+centers[-1])/2
    for a, yy in zip(aliases, centers):
        d.line([(210, middle), (565, yy)], fill=line, width=3)
    for a, yy in zip(aliases, centers):
        oval(d, 1030, yy, cases[a])
    actor(d, 145, middle, label)
    y = centers[-1]+210
center(d, 900, 2944, '实线为角色参与关系；共用与条件关系另见子图。', 31)
im.save(HERE / '02_三角色目标总览_v0.8.png')

im = Image.new('RGB', (2050, 1540), 'white')
d = ImageDraw.Draw(im)
center(d, 1025, 42, 'VetRules 共用与条件关系 · v0.8', 46)
d.rectangle((255, 100, 2000, 1420), outline=line, width=4)
center(d, 1110, 133, 'VetRules 系统边界（四个代表目标）', 34)
positions = {'Q': (590, 280), 'W': (590, 555), 'P': (590, 830), 'A': (590, 1105),
             'Auth': (1510, 300), 'Guard': (1510, 840), 'Hit': (1510, 1240)}
labels = {alias: label.replace('\\n', '\n') for label, alias in
          re.findall(r'usecase "([^"]+)" as (\w+)', blocks[1])}
labels.update({'Q':'UC-02.1 查询\n接诊病历','W':'UC-02.2 记录或\n更正诊疗',
               'P':'UC-03 追加处方\n与用药更正','A':'UC-04.1 采纳建议\n入病历',
               'Auth':'UC-S01 校验当前身份\n与数据范围','Hit':'UC-S03 记录运行护栏\n命中告警'})
assoc = re.findall(r'^Doctor -- (\w+)$', blocks[1], re.M)
rels = re.findall(r'^(\w+) \.\.> (\w+) : <<(include|extend)>>', blocks[1], re.M)
assert len(assoc) == 4 and len(rels) == 8
rx, ry = 310, 72
for a in assoc:
    x, yy = positions[a]
    d.line([(185, 690), (x-rx, yy)], fill=line, width=3)

def boundary(src, dst):
    x, y = positions[src]
    a, b = positions[dst]
    dx, dy = a-x, b-y
    scale = 1/math.sqrt((dx/rx)**2+(dy/ry)**2)
    return (x+dx*scale, y+dy*scale), (a-dx*scale, b-dy*scale)

def dashed_arrow(p, q):
    dx, dy = q[0]-p[0], q[1]-p[1]
    length = math.hypot(dx, dy)
    for k in range(0, int(length), 20):
        a, b = k/length, min(k+12, length)/length
        d.line([(p[0]+dx*a,p[1]+dy*a),(p[0]+dx*b,p[1]+dy*b)], fill=line, width=3)
    angle = math.atan2(dy, dx)
    for offset in (-0.45, 0.45):
        d.line([q,(q[0]-20*math.cos(angle+offset),q[1]-20*math.sin(angle+offset))],fill=line,width=3)

for src, dst, kind in rels:
    p, q = boundary(src, dst)
    dashed_arrow(p, q)
    if kind == 'include':
        # 标签置于靠近源节点的位置，保留线条方向可见。
        x, y = p[0]*0.65+q[0]*0.35, p[1]*0.65+q[1]*0.35
        text = '<<include>>'
        box=d.textbbox((x, y),text,font=fonts[25])
        d.rectangle((box[0]-5,box[1]-3,box[2]+5,box[3]+3),fill='white')
        d.text((x,y),text,font=fonts[25],fill=ink)
    else:
        center(d, 1790, 1040, '<<extend>>\n[运行护栏命中]', 28)
for alias, (x, y) in positions.items():
    oval(d, x, y, labels[alias], rx, ry, 38)
actor(d, 120, 690, '医生账号')
center(d, 1025, 1470, '全部人工目标必经授权；命中时必须记录告警并拒绝违规写入。', 31)
im.save(HERE / '03_共用与条件关系_v0.8.png')
print('已生成两幅用例PNG和与03一致的两份PlantUML文本图源。')
