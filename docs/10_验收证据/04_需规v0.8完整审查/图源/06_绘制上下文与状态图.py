"""依据03的Mermaid语义绘制Word插图，保存原图源；非Mermaid编译器。"""
from pathlib import Path
import re, math
from PIL import Image, ImageDraw, ImageFont

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
doc=(ROOT/'03_需求规格说明书.md').read_text(encoding='utf-8')
blocks=re.findall(r'```mermaid\n(.*?)\n```',doc,re.S)
assert len(blocks)==4
for i,b in enumerate(blocks,1):
    (HERE/f'源图{i:02}.mmd').write_text(b+'\n',encoding='utf-8')
FONT='C:/Windows/Fonts/msyh.ttc'
ink='#19324a'; blue='#edf5fa'; edge='#4d6376'
fonts={n:ImageFont.truetype(FONT,n) for n in (28,31,34,38,42,46)}

def canvas(w,h,title):
    im=Image.new('RGB',(w,h),'white'); d=ImageDraw.Draw(im)
    center(d,w/2,45,title,46)
    return im,d
def center(d,x,y,t,size=34):
    box=d.multiline_textbbox((0,0),t,font=fonts[size],spacing=8,align='center')
    d.multiline_text((x-(box[2]-box[0])/2,y-(box[3]-box[1])/2),t,font=fonts[size],fill=ink,spacing=8,align='center')
def box(d,x,y,w,h,t):
    d.rounded_rectangle((x-w/2,y-h/2,x+w/2,y+h/2),radius=20,fill=blue,outline=edge,width=3)
    center(d,x,y-4,t)
def arrow(d,points,label=None,at=None,dashed=False):
    for p,q in zip(points,points[1:]):
        if dashed:
            dx,dy=q[0]-p[0],q[1]-p[1]; length=math.hypot(dx,dy)
            for k in range(0,int(length),22):
                a,b=k/length,min(k+13,length)/length
                d.line([(p[0]+a*dx,p[1]+a*dy),(p[0]+b*dx,p[1]+b*dy)],fill=edge,width=3)
        else:d.line([p,q],fill=edge,width=3)
    p,q=points[-2:]; a=math.atan2(q[1]-p[1],q[0]-p[0])
    for delta in (-.45,.45):d.line([q,(q[0]-21*math.cos(a+delta),q[1]-21*math.sin(a+delta))],fill=edge,width=3)
    if label:center(d,*at,label,28)
def initial(d,x,y):d.ellipse((x-12,y-12,x+12,y+12),fill=ink)

im,d=canvas(1700,1120,'业务上下文与用户边界')
d.rectangle((1050,180,1650,990),outline=edge,width=3)
center(d,1350,230,'VetRules 系统边界',38)
box(d,1350,580,440,210,'界面、服务、存储\n与消息处理')
items=[(240,'宠物主人\n信息提供者'),(450,'前台账号'),(660,'医生账号'),(870,'管理账号')]
for y,t in items:box(d,250,y,380,130,t)
arrow(d,[(250,305),(250,385)],'线下提供到院信息',(560,347),True)
for y,lab in [(450,'本店该前台接待资料'),(660,'本店该医生负责病历及告警'),(870,'本店治理、汇总、脱敏元数据')]:
    arrow(d,[(440,y),(1050,y)],lab,(735,y-32))
box(d,735,1040,540,100,'建议来源：方案待定')
arrow(d,[(1005,1040),(1350,1040),(1350,990)],'仅提供建议，无病历写权限',(1265,1090),True)
im.save(HERE/'07_业务上下文_v0.8.png')

im,d=canvas(1800,1260,'规则与模板候选审批启用')
pos={'Draft':(350,300),'Review':(900,300),'Rejected':(1450,300),'Approved':(900,750),'Active':(1450,750)}
for k,t in [('Draft','草案'),('Review','待审批'),('Rejected','已驳回'),('Approved','已批准'),('Active','已启用')]:box(d,*pos[k],300,105,t)
initial(d,100,300);arrow(d,[(112,300),(200,300)])
arrow(d,[(500,300),(750,300)],'校验通过\n并提交',(625,232))
arrow(d,[(1050,300),(1300,300)],'人工驳回',(1175,252))
arrow(d,[(900,353),(900,697)],'人工批准\n具体内容',(1040,520))
arrow(d,[(1050,750),(1300,750)],'管理手动\n启用成功',(1175,680))
arrow(d,[(1450,248),(1450,125),(350,125),(350,248)],'修改形成新候选',(895,99))
arrow(d,[(750,750),(350,750),(350,353)],'内容修改形成新候选',(520,795))
arrow(d,[(230,248),(180,248),(180,145),(285,145),(285,248)],'校验失败',(160,99))
arrow(d,[(850,803),(850,960),(1010,960),(1010,803)],'启用失败',(1140,940))
center(d,900,1140,'旧生效版本在新版本启用成功前继续有效；历史定义保留。',34)
center(d,900,1200,'基于Mermaid图源绘制；图表示业务状态，不固定内部实现。',28)
im.save(HERE/'08_审批启用状态_v0.8.png')

im,d=canvas(1700,860,'AI建议的两条处理分支')
initial(d,120,380);box(d,450,380,410,125,'待决定')
box(d,1300,260,530,125,'已采纳入病历');box(d,1300,610,530,125,'未采纳并留存')
arrow(d,[(132,380),(245,380)])
arrow(d,[(655,350),(840,350),(840,260),(1035,260)],'实际医生与理由齐全\n且写入成功',(1100,153))
arrow(d,[(655,410),(840,410),(840,610),(1035,610)],'记录不采纳决定',(1100,520))
arrow(d,[(380,443),(380,640),(550,640),(550,443)],'校验或持久化失败',(450,710))
center(d,850,800,'失败保留建议；保存采纳意图不等于已写入病历。',34)
im.save(HERE/'09_AI建议状态_v0.8.png')

im,d=canvas(1800,1390,'同一告警：业务处理与消息投递的两个维度')
d.rounded_rectangle((70,120,1730,1280),radius=20,outline=edge,width=3)
d.line([(70,640),(1730,640)],fill=edge,width=2)
center(d,900,167,'业务处理维度',38)
initial(d,170,390);box(d,560,390,550,115,'未处理并持续提示');box(d,1400,390,520,115,'已记录处理结果')
arrow(d,[(182,390),(285,390)])
arrow(d,[(835,390),(1140,390)],'获准医生提交结果\n并保存成功',(990,275))
arrow(d,[(450,448),(450,550),(650,550),(650,448)],'无权或保存失败',(560,600))
center(d,900,692,'消息投递维度',38)
initial(d,140,950);box(d,430,950,370,115,'持久待发');box(d,970,950,420,115,'待核对或重试');box(d,1510,950,390,115,'已确认送达')
arrow(d,[(152,950),(245,950)])
arrow(d,[(430,892),(430,780),(1510,780),(1510,892)],'收到可靠确认',(970,750))
arrow(d,[(615,950),(760,950)],'不可用或\n结果不明',(690,1037))
arrow(d,[(1180,950),(1315,950)],'核对或\n重试确认',(1250,1037))
arrow(d,[(880,1008),(880,1160),(1060,1160),(1060,1008)],'仍不可用或结果不明',(970,1220))
center(d,900,1330,'送达不等于医生处理完成；两个维度独立保留。',34)
im.save(HERE/'10_告警两维状态_v0.8.png')

# Word中分角色展示同一总览，避免把20目标缩成难辨认的一页。
uml=re.findall(r'```plantuml\n(.*?)\n```',doc,re.S)[0]
cases={alias:label for label,alias in re.findall(r'usecase "([^"]+)" as (\w+)',uml)}
edges=re.findall(r'^(Front|Doctor|Manager) -- (\w+)$',uml,re.M)
for n,(role,label) in enumerate([('Front','前台'),('Doctor','医生'),('Manager','管理')],11):
    ids=[a for r,a in edges if r==role]; h=240+len(ids)*135
    im,d=canvas(1650,h,f'{label}账号目标（同一VetRules边界的分角色视图）')
    d.rectangle((360,110,1600,h-45),outline=edge,width=3)
    mid=(210+(len(ids)-1)*135+210)/2
    d.ellipse((150,mid-55,190,mid-15),outline=ink,width=3)
    d.line([(170,mid-15),(170,mid+35)],fill=ink,width=3)
    d.line([(130,mid+2),(210,mid+2)],fill=ink,width=3)
    d.line([(170,mid+35),(140,mid+80)],fill=ink,width=3);d.line([(170,mid+35),(200,mid+80)],fill=ink,width=3)
    center(d,170,mid+125,f'{label}账号')
    for i,a in enumerate(ids):
        y=210+i*135;d.line([(225,mid),(520,y)],fill=edge,width=3)
        d.ellipse((520,y-48,1510,y+48),outline=edge,fill=blue,width=3)
        center(d,1015,y-4,cases[a],38)
    im.save(HERE/f'{n:02}_{role}目标_Word分图.png')
print('已保存4幅上下文/状态插图、原始mmd和3幅Word分角色用例视图。')
