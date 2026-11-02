"""Render three submission figures from the current T22 implementation outline."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / '04_设计与控制文件' / '23_报告插图'
OUT.mkdir(parents=True, exist_ok=True)
FONT = Path('C:/Windows/Fonts/msyh.ttc')
BOLD = Path('C:/Windows/Fonts/msyhbd.ttc')
NAVY = '#18354E'
BLUE = '#E8F3FB'
GREEN = '#EAF6EF'
AMBER = '#FFF5E5'
STROKE = '#7C99AC'


def font(size, bold=False):
    return ImageFont.truetype(str(BOLD if bold else FONT), size)


def canvas(title, subtitle):
    img = Image.new('RGB', (1800, 980), '#FFFFFF')
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((32, 28, 1768, 950), radius=28, fill='#F8FBFD', outline='#D6E5EE', width=3)
    d.text((82, 66), title, font=font(40, True), fill=NAVY)
    d.text((82, 126), subtitle, font=font(25), fill='#426377')
    return img, d


def box(d, rect, title, lines=(), fill=BLUE):
    d.rounded_rectangle(rect, radius=20, fill=fill, outline=STROKE, width=3)
    x0, y0, x1, _ = rect
    d.text(((x0 + x1 - d.textbbox((0, 0), title, font=font(29, True))[2]) / 2, y0 + 22),
           title, font=font(29, True), fill=NAVY)
    for index, line in enumerate(lines):
        d.text((x0 + 26, y0 + 73 + index * 37), line, font=font(22), fill='#34566B')


def arrow(d, start, end, label=None):
    d.line((*start, *end), fill='#527A92', width=5)
    from math import atan2, cos, sin, pi
    angle = atan2(end[1] - start[1], end[0] - start[0])
    p1 = (end[0] - 18 * cos(angle - pi / 6), end[1] - 18 * sin(angle - pi / 6))
    p2 = (end[0] - 18 * cos(angle + pi / 6), end[1] - 18 * sin(angle + pi / 6))
    d.polygon((end, p1, p2), fill='#527A92')
    if label:
        d.text(((start[0] + end[0]) / 2 + 12, (start[1] + end[1]) / 2 - 32),
               label, font=font(20), fill='#426377')


def system_layers():
    img, d = canvas('图 1  当前实现的系统分层', '运行角色为前台、医生、管理；页面仅负责交互，权限由服务端判定')
    box(d, (70, 230, 395, 560), '同源工作台', ('原生 HTML/CSS/JS', '三角色页面与表单', '会话、CSRF、CSV 接收'))
    box(d, (510, 230, 845, 560), 'Django / DRF', ('可信账号与门店上下文', '统一权限、行级范围', '服务端字段投影'))
    box(d, (960, 230, 1320, 560), '业务服务', ('治理、接诊与临床', '控制包与护栏判定', '审计、告警、受限导出'), GREEN)
    box(d, (1420, 230, 1730, 560), 'MySQL', ('版本与更正历史', '审计、告警及 outbox', '事务持久化'), AMBER)
    arrow(d, (395, 395), (510, 395))
    arrow(d, (845, 395), (960, 395))
    arrow(d, (1320, 395), (1420, 395))
    box(d, (955, 680, 1315, 870), '后台投递与消费', ('按 outbox 引用补发', '消费收据去重'), GREEN)
    box(d, (1420, 680, 1730, 870), 'RabbitMQ', ('只传告警引用', '送达与处理状态分开'), AMBER)
    arrow(d, (1565, 560), (1315, 715))
    arrow(d, (1315, 775), (1420, 775))
    img.save(OUT / '01_系统分层.png', optimize=True)


def data_history():
    img, d = canvas('图 2  当前实现的简化类关系图', '接诊、版本、命中与告警；箭头表示引用或从属，不表示角色权限继承')
    box(d, (72, 244, 370, 425), '门店与账号', ('Store / Account', '前台、医生、管理'))
    box(d, (515, 244, 825, 425), '接诊', ('Encounter', '固定店、接待及接诊医生'), GREEN)
    box(d, (945, 244, 1250, 425), '临床病历', ('ClinicalRecord', '引用接诊与模板'), GREEN)
    box(d, (1390, 244, 1725, 425), '临床修订', ('ClinicalRevision', '按版本保留诊断与理由'), BLUE)
    arrow(d, (370, 330), (515, 330))
    arrow(d, (825, 330), (945, 330))
    arrow(d, (1250, 330), (1390, 330))
    box(d, (515, 570, 825, 810), '接待修订', ('ReceptionRevision', '原资料与更正并存'))
    box(d, (945, 570, 1250, 810), '临床校验',
        ('ClinicalCheck', '绑定接诊与当次策略', '病历关联可为空'), AMBER)
    box(d, (1390, 570, 1725, 810), '命中 → 告警', ('GuardHit → Alert', 'AlertOutbox 持久待发'), AMBER)
    arrow(d, (670, 425), (670, 570))
    arrow(d, (1097, 425), (1097, 570))
    d.text((1113, 483), '可选', font=font(20), fill='#426377')
    arrow(d, (1250, 690), (1390, 690))
    d.text((80, 884), 'MedicationEntry 以新增关联项更正；AuditEvent / AuditTarget 记录操作与受控目标。',
           font=font(23), fill='#426377')
    img.save(OUT / '02_数据与历史.png', optimize=True)


def submission_sequence():
    img, d = canvas('图 3  医生提交病历的关键时序', '成功写入和护栏阻断分别持久化；后台只在事务提交后补发消息引用')
    actors = [('医生', BLUE), ('HTTP接口', BLUE), ('临床服务', GREEN),
              ('护栏检查', AMBER), ('MySQL', GREEN), ('后台/MQ', AMBER)]
    xs = [185, 470, 755, 1040, 1325, 1610]
    for (title, fill), x in zip(actors, xs):
        d.rounded_rectangle((x - 108, 230, x + 108, 315), radius=15,
                            fill=fill, outline=STROKE, width=3)
        width = d.textbbox((0, 0), title, font=font(27, True))[2]
        d.text((x - width / 2, 253), title, font=font(27, True), fill=NAVY)
        for y in range(315, 900, 18):
            d.line((x, y, x, min(y + 9, 900)), fill='#A3B8C6', width=3)
    steps = [
        (0, 1, 372, '提交病历＋幂等键'),
        (1, 2, 440, '核角色、门店与接诊归属'),
        (2, 4, 508, '锁接诊，核模板与版本'),
        (2, 3, 576, '校验字段、规则与红线'),
        (3, 2, 644, '返回通过或阻断'),
        (2, 4, 712, '写修订或命中告警，并记审计'),
        (5, 4, 792, '提交后领取 outbox'),
        (4, 5, 860, '返回持久事件引用'),
    ]
    for origin, target, y, label in steps:
        left, right = xs[origin], xs[target]
        arrow(d, (left, y), (right, y))
        label_width = d.textbbox((0, 0), label, font=font(20))[2]
        d.text(((left + right) / 2 - label_width / 2, y - 30),
               label, font=font(20), fill='#34566B')
    d.text((78, 915), '后台向 RabbitMQ 发布引用；消费收据去重，消息重投不新增业务告警。',
           font=font(21), fill='#426377')
    img.save(OUT / '03_临床提交时序.png', optimize=True)


if __name__ == '__main__':
    system_layers()
    data_history()
    submission_sequence()
    print('Rendered three current-implementation design figures.')
