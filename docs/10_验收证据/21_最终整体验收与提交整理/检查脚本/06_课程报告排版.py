"""Reproducible Chinese course-report layout, applied to generated Word only."""
from copy import deepcopy
from docx import Document
from docx.shared import Cm, Pt
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
        f=s.paragraph_format
        f.first_line_indent=Pt(0)
        f.space_before, f.space_after=Pt(18), Pt(9)
        f.keep_with_next=True
        f.line_spacing=1.25
    for name in ('Source Code','Verbatim Char'):
        if name in doc.styles:
            font(doc.styles[name],9,'宋体','Consolas')
    # Remove the redundant assembled document title and explanatory line.
    for para in list(doc.paragraphs)[:2]:
        para._element.getparent().remove(para._element)
    for para in doc.paragraphs:
        if para.style.name.startswith('Heading'):
            if para.text.startswith(('正文：','附录')):
                para.style=doc.styles['Heading 1']
                para.paragraph_format.page_break_before=True
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
                    for run in para.runs:
                        run.font.size=Pt(10.5)
                        if ri==0: run.bold=True
    # Insert a dedicated cover and TOC before the first body paragraph.
    anchor=doc.paragraphs[0]
    cover=[]
    def add(text,size,bold=False,before=0,after=12):
        p=anchor.insert_paragraph_before(text)
        p.alignment=WD_ALIGN_PARAGRAPH.CENTER
        f=p.paragraph_format
        f.first_line_indent=Pt(0); f.line_spacing=1.3
        f.space_before=Pt(before); f.space_after=Pt(after)
        for r in p.runs:
            r.font.size=Pt(size); r.bold=bold
            r._r.get_or_add_rPr().append(element('rFonts',eastAsia='黑体' if bold else '宋体',ascii='Times New Roman',hAnsi='Times New Roman'))
        cover.append(p)
        return p
    add('软件设计与开发实验',18,True,40,32)
    add('课程实验报告',26,True,20,30)
    add(title,18,True,0,65)
    for label,key in [('学院班级','class'),('姓　　名','name'),('学　　号','student_id'),('指导教师','teacher')]:
        add(label+'：'+identity.get(key,'________________'),14,False,0,16)
    add(identity.get('date','____年__月__日'),14,False,38,0)
    # Section breaks are attached to final cover/TOC paragraphs.
    sect=deepcopy(doc.sections[-1]._sectPr)
    for n in list(sect):
        if n.tag in (qn('w:headerReference'),qn('w:footerReference'),qn('w:titlePg')): sect.remove(n)
    sect.append(element('type',val='nextPage'))
    cover[-1]._p.get_or_add_pPr().append(deepcopy(sect))
    heading=add('目　录',16,True,0,24)
    toc=anchor.insert_paragraph_before()
    toc.paragraph_format.first_line_indent=Pt(0)
    field=element('fldSimple',instr='TOC \\o "1-3" \\h \\z \\u')
    toc._p.append(field)
    toc._p.get_or_add_pPr().append(deepcopy(sect))
    # Remove any Pandoc TOC to avoid duplication.
    for child in list(doc._element.body):
        if child.tag==qn('w:sdt') and list(child.iter(qn('w:instrText'))):
            child.getparent().remove(child)
    doc.save(path)
