from PySide6.QtWidgets import QMainWindow, QMessageBox, QApplication
from PySide6.QtCore import QTimer, Qt, QSettings
from PySide6.QtGui import QIcon

from ui.ConfigWindow_ui import Ui_ConfigWindow
from ui.AnalysisWindow import AnalysisWindow
# from ui.FFTWindow import FFTWindow

from serial.tools import list_ports
from sys import argv
import subprocess
import qdarktheme
import os
import re

class ConfigWindow(QMainWindow, Ui_ConfigWindow):
    """
    Janela de configuração para análise em tempo real de sensores LPG.

    O usuário deve configurar:
    - Modelo da interface.
    - Intervalo do comprimento de onda a ser analisado e interpolado.
    - Intervalo de resolução do comprimento de onda para interpolação.
    - Parâmetros de comunicação (como porta serial ou TCP/IP).
    - Canais a serem analisados (se aplicável).

    """
    def __init__(self):
        super().__init__()

        self.setupUi(self)
        self.retranslateUi(self)

        # --- Atributos da Classe ---
        # Mantém uma referência à janela de análise para evitar que ela seja
        # coletada pelo garbage collector e para garantir que apenas uma
        # instância seja aberta por vez.
        self.analysis_window: AnalysisWindow | None = None
        # Caminho do arquivo de dados selecionado pelo usuário.
        self.file_path: str | None = None
        # Instância compartilhada de PyOSA para evitar conflito de múltiplas instâncias
        self.pyosa = None
        # Instância compartilhada de PyCCT para evitar conflito de múltiplas instâncias
        self.pycct = None
        # Portas dos switches Sercalo (se detectados) - pode haver múltiplos
        self.switch_ports: list[str] = []
        # Tema atual da aplicação, passado para a janela de análise para manter a consistência visual
        self.theme: str = 'dark'
        # Configurações persistentes da UI (somente campos serializáveis)
        self.settings = QSettings('LiTel', 'online_process_timeseries')

        self.setup_connections()
        self._load_settings()
        self.update_coms()

    def setup_connections(self):
        """
        Conecta os sinais dos widgets (eventos) aos seus respectivos slots (métodos).

        """
        self.start_btn.clicked.connect(self.start_analysis)
        self.startfft_btn.clicked.connect(self.start_fft_analysis)
        self.inter_combo.currentTextChanged.connect(self.set_port_options)
        self.refreshTimer = QTimer(self)
        self.refreshTimer.timeout.connect(self.update_coms)
        self.refreshTimer.start(1000) # Atualiza as portas seriais a cada segundo
        self.ch1_radio.toggled.connect(self.channel_toggled)
        self.start_btn.setShortcut('Return') # Atalho do teclado

        for trace_radio in [self.t_hlay.itemAt(i).widget() for i in range(self.t_hlay.count())]:
            trace_radio.toggled.connect(self.trace_toggled)

        for ch in [self.ch_hlay.itemAt(i).widget() for i in range(self.ch_hlay.count())]:
            ch.toggled.connect(self.channel_toggled)

        self.inter_combo.addItems(['IBSEN IMON-512', 'BRAGGMETER FS22DI', 'BRAGGMETER FS22DI HBM', 'THORLABS CCT11', 'THORLABS OSA203'])

    def _load_settings(self):
        """
        Carrega as preferências persistidas da janela de configuração.

        """
        inter = self.settings.value('config/inter', self.inter_combo.currentText(), type=str)
        inter_index = self.inter_combo.findText(inter)
        if inter_index >= 0:
            self.inter_combo.setCurrentIndex(inter_index)

        self.minNm_spin.setValue(self.settings.value('config/min_nm', self.minNm_spin.value(), type=int))
        self.maxNm_spin.setValue(self.settings.value('config/max_nm', self.maxNm_spin.value(), type=int))
        self.resPm_spin.setValue(self.settings.value('config/res_pm', self.resPm_spin.value(), type=int))
        self.ip_lineEdit.setText(self.settings.value('config/ip', self.ip_lineEdit.text(), type=str))

        fiber = self.settings.value('config/fiber', self.fiber_combo.currentText(), type=str)
        fiber_index = self.fiber_combo.findText(fiber)
        if fiber_index >= 0:
            self.fiber_combo.setCurrentIndex(fiber_index)

        self.theme = self.settings.value('config/theme', self.theme, type=str)
        if self.theme not in ('light', 'dark'):
            self.theme = 'dark'
        self._apply_theme()

        self.ch1_radio.setChecked(self.settings.value('config/ch1', self.ch1_radio.isChecked(), type=bool))
        self.ch2_radio.setChecked(self.settings.value('config/ch2', self.ch2_radio.isChecked(), type=bool))
        self.ch3_radio.setChecked(self.settings.value('config/ch3', self.ch3_radio.isChecked(), type=bool))
        self.ch4_radio.setChecked(self.settings.value('config/ch4', self.ch4_radio.isChecked(), type=bool))
        for t, val in enumerate(self.settings.value('config/bragg_traces', [True, False, False, False], type=list)):
            if t < self.t_hlay.count():
                self.t_hlay.itemAt(t).widget().setChecked(str(val).lower() == 'true')
        self.channel_toggled()

    def _apply_theme(self):
        """
        Aplica o tema atual salvo na ConfigWindow.

        """
        if self.theme not in ('light', 'dark'):
            self.theme = 'dark'
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qdarktheme.load_stylesheet(self.theme))

    def _trace_radios(self):
        return [self.t_hlay.itemAt(i).widget() for i in range(self.t_hlay.count())]

    def _current_trace_selection(self):
        return [radio.isChecked() for radio in self._trace_radios()]

    def trace_toggled(self, *_):
        if not any(self._current_trace_selection()):
            self.t0_radio.setChecked(True)

    def _save_settings(self):
        """
        Salva as preferências da janela de configuração.

        Observação: a porta COM/TCP não é persistida por depender do ambiente atual.
        """
        self.settings.setValue('config/inter', self.inter_combo.currentText())
        self.settings.setValue('config/min_nm', int(self.minNm_spin.value()))
        self.settings.setValue('config/max_nm', int(self.maxNm_spin.value()))
        self.settings.setValue('config/res_pm', int(self.resPm_spin.value()))
        self.settings.setValue('config/ip', self.ip_lineEdit.text())
        self.settings.setValue('config/fiber', self.fiber_combo.currentText())
        self.settings.setValue('config/ch1', self.ch1_radio.isChecked())
        self.settings.setValue('config/ch2', self.ch2_radio.isChecked())
        self.settings.setValue('config/ch3', self.ch3_radio.isChecked())
        self.settings.setValue('config/ch4', self.ch4_radio.isChecked())
        self.settings.setValue('config/theme', self.theme)
        if 'BRAGGMETER' in self.inter_combo.currentText():
            self.settings.setValue('config/bragg_traces', self._current_trace_selection())
        self.settings.sync()

    def _get_ipv4_ethernet(self) -> list[str] | None:
        """
        Retorna o endereço IPv4 da interface Ethernet, se disponível.
        """
        cfg = subprocess.check_output('ipconfig', text=True, encoding='cp850')  # evita problemas de acentuação
        linhas = cfg.splitlines()
        section = False
        for linha in linhas:
            if not "Adaptador Ethernet Ethernet:" in linha and not section:
                continue
            section = True

            # mídia desconectada
            if "desconectada" in linha:
                return None

            # obtém IPv4
            ip = re.search(r"IPv4.*?:\s*([\d\.]+)", linha)
            if ip:
                return ip.group(1).split('.')

    def start_analysis(self):
        """
        Inicia a janela de análise com as configurações selecionadas.

        """
        cur_dir = os.path.dirname(os.path.abspath(argv[0]))

        inter = self.inter_combo.currentText()
        osa = None
        try:
            match inter:
                case 'THORLABS CCT11':
                    if self.pycct is None:
                        from sdk.pyCCT import PyCCT
                        dll_path = os.path.join(cur_dir, "sdk", "net48")
                        self.pycct = PyCCT(dll_path)
                    osa = self.pycct
                case 'THORLABS OSA203':
                    if self.pyosa is None:
                        import sdk.pyOSA as pyOSA
                        self.pyosa = pyOSA.initialize()
                    osa = self.pyosa
        except Exception as e:
            QMessageBox.critical(self, "Erro ao inicializar OSA", f"Não foi possível inicializar o dispositivo OSA: {e}")
            QApplication.instance().restoreOverrideCursor()
            return

        config = {
            'inter': inter,
            'range': (self.minNm_spin.value()*1e-9, self.maxNm_spin.value()*1e-9),
            'res': self.resPm_spin.value()*1e-12,
            'port': self.port_combo.currentText(),
            'ip': self.ip_lineEdit.text(),
            'fiber': self.fiber_combo.currentText(),
            'path': self.file_path,
            'sdk': osa,
            'switch_ports': self.switch_ports,  # Lista de todas as portas de switch
            'channels': [i.isChecked() for i in [self.ch_hlay.itemAt(i).widget() for i in range(self.ch_hlay.count())]],
            'bragg_traces': self._current_trace_selection(),
            'theme': self.theme,
        }

        ip = self._get_ipv4_ethernet()
        if not ip:
            QMessageBox.warning(self, "Falha na conexão", "Cabo de rede não identificado, verifique a conexão Ethernet.")
            return

        user_ip = config.get('ip').split('.')
        for x in user_ip:
            if not x.isdigit() or not 0 <= int(x) <= 255:
                QMessageBox.warning(self, "IP inválido", "O endereço IP configurado não é válido.")
                return
            
        for i in range(3):
            if ip[i] != user_ip[i] or ip[3] == user_ip[3]:
                QMessageBox.warning(self, "IP inválido", "O endereço IP configurado não condiz com o IP da interface Ethernet.")
                return

        self._save_settings()
        QApplication.instance().setOverrideCursor(Qt.WaitCursor)

        if self.analysis_window is None:
            self.analysis_window = AnalysisWindow()
            icon = os.path.join(cur_dir, "img", "litel.png")
            self.analysis_window.setWindowIcon(QIcon(icon))
            self.analysis_window.closing.connect(lambda theme: self.on_analysis_window_closed(theme))
            self.analysis_window.load_config(config)
            self.analysis_window.showMaximized()
            self.refreshTimer.stop()
            self.hide()

    def start_fft_analysis(self):
        """
        Inicia a janela de análise FFT com as configurações selecionadas.

        """
        QApplication.instance().setOverrideCursor(Qt.WaitCursor)
        cur_dir = os.path.dirname(os.path.abspath(argv[0]))

        config = {
            'port': self.port_combo.currentText(),
            'fiber': self.fiber_combo.currentText(),
            'path': self.file_path,
            'theme': self.theme,
        }

        self._save_settings()

        if self.analysis_window is None:
            # self.analysis_window = FFTWindow()
            icon = os.path.join(cur_dir, "img", "litel.png")
            self.analysis_window.setWindowIcon(QIcon(icon))
            self.analysis_window.closing.connect(lambda theme: self.on_analysis_window_closed(theme))
            self.analysis_window.load_config(config)
            self.analysis_window.showMaximized()
            self.hide()

    def on_analysis_window_closed(self, theme: str):
        """
        Callback chamado quando a janela de análise é fechada.

        """
        QApplication.instance().restoreOverrideCursor()
        self.analysis_window = None
        self.theme = theme
        self._apply_theme()
        self._save_settings()
        self.show()
        self.refreshTimer.start(1000)

    def bragg(self):
        """
        Configura as opções de porta para o BraggMeter.
        
        """
        self.com_lbl.setEnabled(True)
        self.port_combo.clear()
        self.port_combo.addItem('3500')
        self.port_combo.addItem('3365')
        self.port_combo.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.ip_lbl.setEnabled(True)
        self.ip_lineEdit.setEnabled(True)
        self.startfft_btn.setEnabled(False)
        self.setTraces(True)

    def thorlabs(self):
        """
        Configura as opções de porta para os dispositivos Thorlabs.
        
        """
        self.port_combo.clear()
        self.port_combo.addItem('Não se aplica')
        self.ip_lbl.setEnabled(False)
        self.ip_lineEdit.setEnabled(False)
        self.com_lbl.setEnabled(False)
        self.port_combo.setEnabled(False)
        self.startfft_btn.setEnabled(False)
        self.setTraces(False)

    def setSpins(self, min: int, max: int, minVal: int = None, maxVal: int = None):
        """
        Configura os limites e valores iniciais dos spin boxes de comprimento de onda.
        
        """
        self.minNm_spin.setRange(min, max-1)
        self.maxNm_spin.setRange(min+1, max)
        if minVal:
            self.minNm_spin.setValue(minVal)
        else:
            self.minNm_spin.setValue(min)
        if maxVal:
            self.maxNm_spin.setValue(maxVal)
        else:
            self.maxNm_spin.setValue(max)

    def setTraces(self, state: bool):
        """
        Habilita ou desabilita os traces do BraggMeter.

        """
        for t in self._trace_radios():
            t.setEnabled(state)
        self.t_lbl.setEnabled(state)

    def set_port_options(self, inter: str):
        """
        Ajusta as opções de porta com base no tipo de interface selecionado (serial e TCP/IP).

        """
        match inter:
            case 'IBSEN IMON-512':
                self.ip_lbl.setEnabled(False)
                self.ip_lineEdit.setEnabled(False)
                self.com_lbl.setEnabled(True)
                self.setSpins(1510, 1595)
                self.update_coms()
                self.setTraces(False)
            case 'BRAGGMETER FS22DI':
                self.bragg()
                self.setSpins(1500, 1600)
                self.ip_lineEdit.setText("10.0.0.10")
            case 'BRAGGMETER FS22DI HBM':
                self.bragg()
                self.setSpins(1500, 1600)
                self.ip_lineEdit.setText("192.168.1.19")
            case 'THORLABS CCT11':
                self.thorlabs()
                self.setSpins(350, 700)
            case 'THORLABS OSA203':
                self.thorlabs()
                self.setSpins(1000, 2500, 1450, 1650)

    def channel_toggled(self):
        """
        Habilita o botão de iniciar análise apenas se pelo menos um canal estiver selecionado.

        """
        if any(ch.isChecked() for ch in [self.ch_hlay.itemAt(i).widget() for i in range(self.ch_hlay.count())]):
            self.start_btn.setEnabled(True)
        else:
            self.start_btn.setEnabled(False)

    def update_coms(self):
        """
        Procura por portas seriais com o fabricante FTDI e Silicon Labs.

        """
        ports = list_ports.comports()
        imon = [port for port in ports if 'FTDI' in port.manufacturer]
        switch = [port for port in ports if 'Silicon Labs' in port.manufacturer]
        # Armazena todas as portas de switch detectadas
        self.switch_ports = [port.device for port in switch] if switch else []

        if self.inter_combo.currentText() == 'IBSEN IMON-512':
            self.port_combo.clear()
            if not imon:
                self.port_combo.addItem("Dispositivo não encontrado")
                self.port_combo.setEnabled(False)
                self.start_btn.setEnabled(False)
            else:
                self.port_combo.setEnabled(True)
                self.start_btn.setEnabled(True)
                self.startfft_btn.setEnabled(True)
                for port in imon:
                    self.port_combo.addItem(port.device)

        for ch in [self.ch_hlay.itemAt(i).widget() for i in range(self.ch_hlay.count())]:
            ch.setEnabled(bool(switch))
            if not switch:
                ch.setChecked(False)
        if not switch:
            self.ch1_radio.setChecked(True)
        self.ch_lbl.setEnabled(bool(switch))

    def closeEvent(self, event):
        """
        Salva as preferências persistentes ao fechar a janela.

        """
        self._save_settings()
        super().closeEvent(event)