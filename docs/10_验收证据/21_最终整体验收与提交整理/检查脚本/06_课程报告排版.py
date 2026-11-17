"""Reproducible Chinese course-report layout, applied to generated Word only."""
from copy import deepcopy
import re
from docx.text.paragraph import Paragraph
from docx import Document
from docx.shared import Cm, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn


def element(name, **attrs):
    node = OxmlElement('w:' + name)
    for key, value in attrs.items():
        node.set(qn('w:' + key), str(value))
    return node


def font(style, size, chinese='宋体', latin='Times New Roman'):
    style.font.name = latin
    style.font.size = Pt(size)
    style.font.color.rgb = RGBColor(0,0,0)
    style.element.get_or_add_rPr().append(element('rFonts', eastAsia=chinese, ascii=latin, hAnsi=latin))


def format_report(path, title, identity):
    doc = Document(path)
    for section in doc.sections:
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.top_margin = section.bottom_margin = Cm(2.5)
        section.left_margin, section.right_margin = Cm(3), Cm(2.5)
        section.header_distance = section.footer_distance = Cm(1.25)
    for style in doc.styles:
        if style.type == 1:
            font(style, 12)
            style.paragraph_format.space_after = Pt(0)
    for name in ('Normal', 'Body Text', 'First Paragraph'):
        if name in doc.styles:
            fmt = doc.styles[name].paragraph_format
            fmt.first_line_indent = Pt(24)
            fmt.line_spacing = 1.5
            fmt.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            fmt.widow_control = True
    for level, size in ((1,16),(2,14),(3,12),(4,12),(5,12),(6,12)):
        s = doc.styles['Heading ' + str(level)]
        font(s,size,'黑体')
        s.font.bold = True
        s.font.italic = False
        f=s.paragraph_format
        f.first_line_indent=Pt(0)
        f.space_before, f.space_after=Pt(18), Pt(9)
        f.keep_with_next=True
        f.line_spacing=1.25
    for name in ('Source Code','Verbatim Char'):
        if name in doc.styles:
            font(doc.styles[name],9,'宋体','Consolas')
    figure_number = 0
    for para in list(doc.paragraphs):
        if para.text in ('课程名称：软件设计与开发实验','个人身份信息见总报告封面。','指导教师及报告日期见总报告封面。'):
            para._element.getparent().remove(para._element)
            continue
        for run in para.runs:
            if '**' in run.text:
                run.text = run.text.replace('**','')
                run.bold = True
        if para.style.name.startswith('Heading'):
            if para.style.name == 'Heading 1':
                para.style=doc.styles['Heading 1']
                para.paragraph_format.page_break_before=para.text.startswith(('参考文献','附录'))
            elif para.style.name == 'Heading 3' and para.text.startswith(('T22 ', 'T22_', '个人研究分析报告', '系统设计报告', '测试与实测报告', '运行与提交说明')):
                # Keep content; hide duplicate source-document titles from TOC.
                para.style=doc.styles['Subtitle']
        if para.style.name in ('Source Code',):
            para.paragraph_format.first_line_indent=Pt(0)
            para.paragraph_format.line_spacing=1
            para.paragraph_format.space_after=Pt(2)
            para._p.get_or_add_pPr().append(element('shd',fill='F3F3F3'))
        if para._p.xpath('.//w:drawing'):
            para.alignment=WD_ALIGN_PARAGRAPH.CENTER
            para.paragraph_format.first_line_indent=Pt(0)
            para.paragraph_format.keep_with_next=True
        for run in para.runs:
            for node in run._r.xpath('.//wp:extent'):
                max_width=int(Cm(15.5))
                if int(node.get('cx')) > max_width:
                    ratio=max_width/int(node.get('cx'))
                    node.set('cx',str(max_width)); node.set('cy',str(int(int(node.get('cy'))*ratio)))
                if int(node.get('cy')) > int(Cm(17)):
                    ratio=int(Cm(17))/int(node.get('cy'))
                    node.set('cy',str(int(Cm(17)))); node.set('cx',str(int(int(node.get('cx'))*ratio)))
    for table in doc.tables:
        table.autofit=True
        borders=element('tblBorders')
        for edge in ('top','left','bottom','right','insideH','insideV'):
            borders.append(element(edge,val='single',sz=4,color='808080'))
        table._tbl.tblPr.append(borders)
        for ri,row in enumerate(table.rows):
            row._tr.get_or_add_trPr().append(element('cantSplit'))
            if ri==0: row._tr.get_or_add_trPr().append(element('tblHeader'))
            for cell in row.cells:
                for para in cell.paragraphs:
                    f=para.paragraph_format
                    f.first_line_indent=Pt(0); f.line_spacing=1.15
                    f.space_before=f.space_after=Pt(3)
                    if ri==0: f.keep_with_next=True
                    for run in para.runs:
                        run.font.size=Pt(10.5)
                        if ri==0: run.bold=True
    # Front matter is explicit, independent of paragraph positions and image order.
    anchor=doc.paragraphs[0]
    def add(text,size,bold=False,before=0,after=12):
        p=anchor.insert_paragraph_before(text)
        p.alignment=WD_ALIGN_PARAGRAPH.CENTER
        f=p.paragraph_format
        f.first_line_indent=Pt(0); f.line_spacing=1.3
        f.space_before=Pt(before); f.space_after=Pt(after)
        for r in p.runs:
            r.font.size=Pt(size); r.bold=bold
            r._r.get_or_add_rPr().append(element('rFonts',eastAsia='黑体' if bold else '宋体',ascii='Times New Roman',hAnsi='Times New Roman'))
        return p
    def section_after(p):
        sect=deepcopy(doc.sections[-1]._sectPr)
        for n in list(sect):
            if n.tag in (qn('w:headerReference'),qn('w:footerReference'),qn('w:titlePg')): sect.remove(n)
        sect.append(element('type',val='nextPage'))
        p._p.get_or_add_pPr().append(sect)
    add('课 程 实 验 报 告',26,True,70,42)
    add('社区宠物医院门诊病历与护栏规则系统',17,True,0,8)
    add('的设计与实现',17,True,0,55)
    table=doc.add_table(rows=0, cols=2)
    anchor._p.addprevious(table._tbl)
    table.autofit=False
    borders=element('tblBorders')
    for edge in ('top','left','bottom','right','insideH','insideV'): borders.append(element(edge,val='nil'))
    table._tbl.tblPr.append(borders)
    table.columns[0].width=Cm(3.2); table.columns[1].width=Cm(10.5)
    for label,value in [('课程名称','软件设计与开发实验'),('专业班级',identity.get('class','')),('学    号',identity.get('student_id','')),('姓    名',identity.get('name','')),('指导教师',identity.get('teacher','')),('报告日期',identity.get('date',''))]:
        cells=table.add_row().cells
        cells[0].text=label+'：'; cells[1].text=value
        for cell in cells:
            for p in cell.paragraphs:
                p.paragraph_format.first_line_indent=Pt(0)
                p.paragraph_format.space_after=Pt(16)
                for r in p.runs: r.font.size=Pt(14)
    end=anchor.insert_paragraph_before(); section_after(end)
    add('摘　要',16,True,0,24)
    abstract=anchor.insert_paragraph_before('本项目面向社区宠物医院门诊业务，围绕病历版本、角色权限、规则护栏和访问审计实现单机分层系统。系统采用Django/DRF、MySQL与RabbitMQ，将控制文件校验、人工审批、只追加更正及持久消息补发连接到统一服务端入口。通过功能回归、异常注入和双机活跃会话测试，检验授权、事务与导出的完整性。最终回归275项通过，生产Python语句覆盖率89.97%；在360独立会话、15—25秒操作间隔的600秒窗口内，10778次请求零失败，七类完整响应P95为9.997—50.034毫秒。结果适用于披露的会话模型。报告同时分析AI辅助开发中的需求澄清、规格约束、人工决策与证据核验方法。')
    key=anchor.insert_paragraph_before('关键词：门诊病历；权限隔离；规则护栏；事务审计；AI辅助开发')
    key.paragraph_format.page_break_after=False
    toc_title=add('目　录',16,True,0,24)
    toc_title.paragraph_format.page_break_before=True
    toc=anchor.insert_paragraph_before()
    toc.paragraph_format.first_line_indent=Pt(0)
    toc._p.append(element('fldSimple',instr='TOC \\o "1-2" \\h \\z'))
    section_after(toc)
    anchor.paragraph_format.page_break_before=False
    toc.paragraph_format.keep_with_next=False
    for child in list(doc._element.body):
        if child.tag==qn('w:sdt') and list(child.iter(qn('w:instrText'))): child.getparent().remove(child)
    doc.save(path)
