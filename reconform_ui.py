"""Three explicit steps for conforming an edited proxy XML to originals."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from PyQt6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QGroupBox,QLineEdit,QPushButton,QLabel,QFileDialog,QMessageBox
from core import AnalysisError
from proxy_workflow import conform_edited_xml

class ReconformDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent);self.setWindowTitle('原片回套');self.resize(740,330)
        self.xml_path='';self.map_paths=[];self.generated_path=None
        layout=QVBoxLayout(self)
        group=QGroupBox('1 选择代理剪辑XML');row=QHBoxLayout(group)
        self.xml_field=QLineEdit();self.xml_field.setReadOnly(True);self.xml_field.setPlaceholderText('达芬奇导出的 FCPXML / Final Cut Pro 7 XML')
        self.xml_button=QPushButton('选择XML…');self.xml_button.clicked.connect(lambda:self.guarded(self.choose_xml))
        row.addWidget(self.xml_field);row.addWidget(self.xml_button);layout.addWidget(group)
        group=QGroupBox('2 选择对应JSON');row=QHBoxLayout(group)
        self.maps_field=QLineEdit();self.maps_field.setReadOnly(True);self.maps_field.setPlaceholderText('proxy_original_map.json；跨场混剪可多选')
        self.maps_button=QPushButton('选择JSON…');self.maps_button.setEnabled(False);self.maps_button.clicked.connect(lambda:self.guarded(self.choose_maps))
        row.addWidget(self.maps_field);row.addWidget(self.maps_button);layout.addWidget(group)
        group=QGroupBox('3 生成原片XML');row=QHBoxLayout(group)
        self.output_field=QLineEdit();self.output_field.setReadOnly(True);self.output_field.setPlaceholderText('生成时选择保存位置，默认桌面')
        self.generate_button=QPushButton('生成原片XML…');self.generate_button.setEnabled(False);self.generate_button.clicked.connect(lambda:self.guarded(self.generate))
        row.addWidget(self.output_field);row.addWidget(self.generate_button);layout.addWidget(group)
        self.status=QLabel('');self.status.setWordWrap(True);layout.addWidget(self.status)
        close=QPushButton('关闭');close.clicked.connect(self.reject);layout.addWidget(close)
    def guarded(self,action):
        try:action()
        except Exception as exc:QMessageBox.warning(self,'原片回套未完成',str(exc))
    def choose_xml(self):
        path,_=QFileDialog.getOpenFileName(self,'第1步：选择代理剪辑XML','','XML (*.xml *.fcpxml)')
        if not path:return
        root=ET.parse(path).getroot()
        if root.tag not in ('xmeml','fcpxml') or root.find('.//sequence') is None:
            raise AnalysisError('请选择达芬奇导出的FCPXML或Final Cut Pro 7 XML，不是DRT。')
        self.xml_path=path;self.xml_field.setText(path)
        self.map_paths=[];self.maps_field.clear();self.output_field.clear();self.generated_path=None
        self.maps_button.setEnabled(True);self.generate_button.setEnabled(False)
        self.status.setText('请选择与这条时间线对应的JSON。')
    def choose_maps(self):
        if not self.xml_path:raise AnalysisError('请先选择代理剪辑XML。')
        paths,_=QFileDialog.getOpenFileNames(self,'第2步：选择对应JSON（可多选）','','JSON (*.json)')
        if not paths:return
        for path in paths:
            data=json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('records'),list) or not data['records']:
                raise AnalysisError('请选择导出代理时间线时生成的proxy_original_map.json。')
            if data.get('fps') not in (24,25,30) or any(not isinstance(row,dict) or not {'proxy_path','proxy_start','frames'}<=row.keys() for row in data['records']):
                raise AnalysisError('JSON对应表格式不完整。')
        self.map_paths=paths;self.maps_field.setText('；'.join(paths));self.maps_field.setToolTip('\n'.join(paths))
        self.output_field.clear();self.generated_path=None;self.generate_button.setEnabled(True)
        self.status.setText(f'已选择{len(paths)}个对应表，可以生成原片XML。')
    def generate(self):
        if not self.xml_path or not self.map_paths:raise AnalysisError('请先完成第1步和第2步。')
        default=Path.home()/'Desktop'/(Path(self.xml_path).stem+'_ORIGINAL'+Path(self.xml_path).suffix)
        output,_=QFileDialog.getSaveFileName(self,'第3步：保存原片XML',str(default),'XML (*.xml *.fcpxml)')
        if not output:return
        self.generate_button.setEnabled(False)
        try:report=conform_edited_xml(self.xml_path,self.map_paths,output)
        finally:self.generate_button.setEnabled(True)
        self.generated_path=output;self.output_field.setText(output)
        links=report.get('audio_link_normalization',{});link_note=''
        if links.get('normalized_multicam_clips'):
            link_note=f'\n已恢复{links["normalized_multicam_clips"]}段音视频链接。'
        if links.get('skipped'):
            link_note+=f'\n{len(links["skipped"])}段复杂声音编辑已保留，请在达芬奇核查链接；详情见同名review.json。'
        if report.get('resolve_import_helper'):
            link_note+='\n分离声道请通过达芬奇“工作区 → 脚本 → 三木_导入多机位XML”导入，XML与同名.resolve.lua放在一起。'
        self.status.setText(f'已生成原片XML：替换{report["replaced_file_definitions"]}个源文件，保留{len(report["unmapped_retained"])}个未映射文件。{link_note}\n{output}')
