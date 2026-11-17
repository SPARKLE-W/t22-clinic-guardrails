"""Generate three report diagrams from the current VetRules design (15 cm wide)."""
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib.font_manager import FontProperties

OUT = Path(__file__).resolve().parent
FONT = FontProperties(fname='C:/Windows/Fonts/msyh.ttc', size=10)
BLUE = '#244C70'

def canvas(height):
    fig, ax = plt.subplots(figsize=(15/2.54, height/2.54), dpi=300)
    fig.patch.set_facecolor('white')
    fig.subplots_adjust(left=.035, right=.965, top=.97, bottom=.03)
    ax.set(xlim=(0, 10), ylim=(0, 10))
    ax.axis('off')
    return fig, ax

def box(ax, x, y, w, h, text, blue=False, size=10):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0.04,rounding_size=.10',
        linewidth=1, edgecolor=BLUE if blue else '#65717B', facecolor='#EAF1F7' if blue else '#F5F6F7'))
    ax.text(x+w/2,y+h/2,text,ha='center',va='center',fontproperties=FONT,fontsize=size,color='#172B3A',linespacing=1.6)

def arrow(ax, a, b, text=None):
    ax.annotate('', xy=b,xytext=a,arrowprops=dict(arrowstyle='->',color=BLUE,lw=1.15,shrinkA=2,shrinkB=2))
    if text:
        ax.text((a[0]+b[0])/2+.12,(a[1]+b[1])/2,text,fontproperties=FONT,fontsize=8,color=BLUE,va='center')

def save(fig,name):
    fig.savefig(OUT/name,dpi=300,facecolor='white')
    plt.close(fig)

fig, ax = canvas(13)
box(ax,.5,8.6,9,1,'同源工作台｜前台 · 医生 · 管理\n原生 HTML / CSS / JavaScript',True)
box(ax,.5,6.8,9,1,'HTTP接口与统一权限入口\n会话 / CSRF · 角色 / 门店 / 接诊归属')
box(ax,.5,5,9,1,'应用服务｜接诊与临床 · 治理 · 审计导出 · 分工\n显式事务编排与操作幂等',True)
box(ax,.5,3.2,9,1,'领域策略｜模板版本 · 状态约束 · 护栏判定\n已审批并手动启用的控制包')
box(ax,.5,.9,4,1.35,'MySQL\n病历历史 · 审计 · 告警 / outbox',True,9)
box(ax,5.2,.9,4.3,1.35,'后台投递 / 消费 — RabbitMQ\n消息仅传事件引用',True,9)
for y in (8.6,6.8,5): arrow(ax,(5,y),(5,y-.8))
arrow(ax,(3,3.2),(2.5,2.25)); arrow(ax,(7,3.2),(7.3,2.25))
arrow(ax,(5.2,1.55),(4.5,1.55))
ax.text(5,.22,'Web与后台进程共用代码库；浏览器不直连数据库或消息队列',ha='center',fontproperties=FONT,fontsize=8,color='#58636C')
save(fig,'04_总报告架构.png')

fig, ax = canvas(14)
box(ax,1.1,8.45,7.8,1.1,'Encounter｜一次接诊\n固定门店、接待前台、接诊医生',True)
box(ax,.25,5.9,4.25,1.25,'ClinicalRecord｜病历\n引用原模板；保存当前修订指针',True,9)
box(ax,5.5,5.9,4.25,1.25,'Suggestion / Decision\n建议来源与一次终态决定',False,9)
box(ax,.25,3.35,4.25,1.25,'RecordRevision｜病历历史\n新修订追加；旧内容保留',False,9)
box(ax,5.5,3.35,4.25,1.25,'MedicationEntry｜用药历史\n原项 ← 关联更正项（只追加）',False,9)
box(ax,1.1,.8,7.8,1.25,'ClinicalCheck → GuardHit → Alert\n命中与告警关联持久 outbox；投递收据独立保存',True,9)
arrow(ax,(3,8.45),(2.4,7.15),'1:0..1'); arrow(ax,(7,8.45),(7.6,7.15),'1:N')
arrow(ax,(2.4,5.9),(2.4,4.6),'1:N'); arrow(ax,(4.5,6.3),(7.6,4.6))
# Suggestion and medication are separate children of the encounter/record.
arrow(ax,(5,8.45),(5,2.05))
ax.text(5,.2,'箭头表示从属或引用；所有临床子对象均回到真实接诊核验权限',ha='center',fontproperties=FONT,fontsize=8,color='#58636C')
save(fig,'05_总报告数据关系.png')

fig, ax = canvas(17)
box(ax,1.3,8.95,7.4,.75,'医生请求 → 可信身份、归属授权、操作幂等',True)
box(ax,1.3,7.6,7.4,.85,'事务内锁定接诊\n核对状态、期望版本、原模板及生效策略',False,9)
box(ax,2.3,6.25,5.4,.75,'guard.validate（候选内容，规则快照）',True,9)
arrow(ax,(5,8.95),(5,8.45)); arrow(ax,(5,7.6),(5,7))
box(ax,.25,4.4,4.3,1.05,'通过：追加临床版本\n与相应访问审计同事务提交',True,9)
box(ax,5.45,4.4,4.3,1.05,'阻断：不写违规临床内容\n可靠保存拒绝、命中、告警、outbox',False,9)
arrow(ax,(3.4,6.25),(2.4,5.45),'通过'); arrow(ax,(6.6,6.25),(7.6,5.45),'命中')
box(ax,.25,2.85,4.3,.75,'提交成功后返回确定结果',False,9)
box(ax,5.45,2.85,4.3,.9,'后台发布事件引用\n发布确认不等于消费完成',True,9)
arrow(ax,(2.4,4.4),(2.4,3.6)); arrow(ax,(7.6,4.4),(7.6,3.75))
box(ax,5.45,1.2,4.3,1,'消费收据落库后 ack\n重投复用 event_id，不新增告警',False,9)
arrow(ax,(7.6,2.85),(7.6,2.2))
box(ax,.25,1.2,4.3,1,'医生独立处理告警\n核权并保存账号、时间、结果',False,9)
arrow(ax,(5.45,1.7),(4.55,1.7))
ax.text(5,.3,'故障或持久化未确认：如实报告；队列送达不自动关闭业务告警',ha='center',fontproperties=FONT,fontsize=8,color='#58636C')
save(fig,'06_总报告提交时序.png')

