from PySide6.QtWidgets import (QMainWindow, QMessageBox, QGraphicsRectItem, QInputDialog, QDialog, QPushButton, 
    QMenu, QApplication, QFormLayout, QSpinBox, QDoubleSpinBox, QDialogButtonBox, QLineEdit, QCompleter, QTabWidget, QWidget, QVBoxLayout)
from PySide6.QtGui import QColor, QLinearGradient, QBrush, QIcon, QPalette, QGradient
from PySide6.QtCore import Signal, QThread, QTimer, Qt, QLocale, QSettings

from ui.AnalysisWindow_ui import Ui_AnalysisWindow
from ui.toggle import ToggleSwitch
from core.processing import find_resonant_wavelength, find_wavelength_peaks, preprocess_plot_data
from core.data_acquisition import DataAcquisition
from iobound.file_manager import append_samples, prompt_save_file, prompt_open_file, load_samples

from scipy.signal import windows
from scipy.interpolate import interp1d
from datetime import datetime
import pyqtgraph as pg
from sys import argv
import numpy as np
import webbrowser
import time
import os

import qdarktheme

import logging
logger = logging.getLogger(__name__)

class AnalysisWindow(QMainWindow, Ui_AnalysisWindow):
    """
    Janela de Análise Interativa.

    Nesta tela, é feita a leitura e exibição dos dados espectrais. O usuário
    pode interagir com os gráficos para selecionar a região de interesse,
    visualizar os picos detectados em uma variação ao longo do tempo ou
    em um box plot.
    
    """
    # Sinal para indicar à ConfigWindow que esta janela está sendo fechada
    closing = Signal(str)
    request_data_signal = Signal(int, int, object) # (mean_samples, channel, bragg_traces)
    stop_worker_signal = Signal()

    def __init__(self):
        super().__init__()

        # Configura a interface do usuário definida no Qt Designer
        self.setupUi(self)
        self.retranslateUi(self)

        # --- Atributos da Classe ---
        # Dicionário para armazenar as configurações recebidas da MainWindow
        self.config_data: dict | None = None
        # Lista para armazenar os dados dos espectros lidos
        # Formato: [(wavelength_array, power_array), ...]
        self.spectra_data: list | None = None
        # Objeto para a Região de Interesse (ROI) no gráfico espectral
        self.roi_region = pg.LinearRegionItem(orientation=pg.LinearRegionItem.Vertical)
        # Objeto para a Região de Interesse (ROI) dos dados processados (no gráfico temporal)
        self.temporal_roi_region = pg.LinearRegionItem(orientation=pg.LinearRegionItem.Vertical, brush=(0, 255, 0, 30))

        # Limites da ROI para o processamento de picos
        self.roi_range: list | None = None
        # Parâmetros de detecção de picos no modo FBG
        self.peak_detection_params = {
            'prominence': 300.0,
            'width': 2.0,
            'distance': 40,
        }
        # Rastreador de cores para picos FBG: mapeia wavelength (em metros) para cor
        # Garante que o mesmo pico mantém a mesma cor ao longo do tempo
        self.fbg_peak_color_map: dict[float, tuple] = {}
        # Número máximo de entradas no mapa de cores; evita crescimento ilimitado
        # com drift de picos ao longo de horas de operação.
        self._MAX_PEAK_COLORS: int = 64
        # Referências directas às InfiniteLines actuais no spectraPlotWidget;
        # evita iterar sobre todos os items do plot para removê-las.
        self._peak_marker_lines: list = []
        # Comprimentos de onda fixos para interpolação (em metros)
        self.fixed_wavelengths: np.ndarray | None = None
        # Dicionário de listas para armazenar os resultados processados
        self.results_df = self._empty_result_store()
        # Dicionário com os dados das amostras para o box plot
        self.samples = {}
        # Tempo de exposição (µs)
        self.exposure_time: float = 0.0
        # Intervalo entre amostras (ms)
        self.sample_rate: int = 0
        # Nome da amostra contínua
        self.sample_name: str | None = None
        # Timer para parar a aquisição contínua
        self.continuous_timer: QTimer | None = None
        # Timer para flush periódico dos dados no modo contínuo
        self.flush_timer: QTimer | None = None
        # Buffer para salvar em lote no modo contínuo
        self.pending_hdf5 = self._empty_result_store()
        # Tamanho do lote para flush no modo contínuo
        self.flush_batch_size: int = 25
        # Intervalo para flush automático no modo contínuo (ms)
        self.flush_interval_ms: int = 15000
        # Limite de pontos mantidos em memória para evitar lentidão da UI
        self.max_live_points: int = 5000
        # Unidade do eixo x (comprimento de onda)
        self.xUnit: str = 'nm'
        # Unidade fixa para temporal/boxplot (não muda com actions de unidade)
        self.resultUnit: str = 'nm'
        # Tema atual persistido
        self.theme: str = 'dark'
        self.theme_colors = {}
        # Cores fixas para os canais exibidos na tab Merge
        self.merge_channel_colors = [
            QColor(139, 0, 0),
            QColor(0, 152, 0),
            QColor(0, 0, 188),
            QColor(255, 170, 0),
        ]
        # Dicionário para armazenar os espectros fixados
        self.fixed_traces = {}
        # Lista de botões de fixação atualmente ativos (para manter o estado ao mudar de aba)
        self.active_traces = []
        # Botão de aviso (criado dinamicamente em _show_warning)
        self.warning_btn: QPushButton | None = None
        # Mapa mensagem -> índice do canal que exibiu o popup
        self._warning_popup_shown: dict[str, int] = {}
        # Flag para indicar se a aquisiçao está ativa
        self._running: bool = False
        # Caminho para o arquivo de dados selecionado pelo usuário
        self.file_path: str | None = None
        # Lista com as mensagens de erro recebidas
        self.error_messages: list = []
        # Estado por aba/canal para manter análises independentes
        self.channel_states: dict[int, dict] = {}
        self.active_channel_idx: int = 0
        # Lista dos canais habilitados para aquisição cíclica
        self.enabled_channels: list[int] = []
        # Índice da aba trace atualmente em exibição
        self.in_view_trace: int | None = None

        # Método de janela aplicado no plot (None desabilita)
        self.window: str | None = None
        self.window_methods = {
            'Tukey': lambda m, *a: windows.tukey(m, sym=False),
            'Triangular': lambda m, *a: windows.triang(m, sym=False),
            'Taylor': lambda m, *a: windows.taylor(m, sym=False),
            'Parzen': lambda m, *a: windows.parzen(m, sym=False),
            'Nuttall': lambda m, *a: windows.nuttall(m, sym=False),
            'Lanczos': lambda m, *a: windows.lanczos(m, sym=False),
            'Hann': lambda m, *a: windows.hann(m, sym=False),
            'Hamming': lambda m, *a: windows.hamming(m, sym=False),
            'Gaussian': lambda m, std: windows.gaussian(m, std, sym=False),
            'Flat top': lambda m, *a: windows.flattop(m, sym=False),
            'Cosine': lambda m, *a: windows.cosine(m, sym=False),
            'Boxcar': lambda m, *a: windows.boxcar(m, sym=False),
            'Bohman': lambda m, *a: windows.bohman(m, sym=False),
            'Blackman-Harris 4-term': lambda m, *a: windows.blackmanharris(m, sym=False),
            'Blackman': lambda m, *a: windows.blackman(m, sym=False),
            'Bartlett': lambda m, *a: windows.bartlett(m, sym=False),
            'Bartlett-Hann': lambda m, *a: windows.barthann(m, sym=False),
        }
        # Parametros do filtro Savitzky-Golay aplicados no plot
        self.savgol_window_points: int = 51
        self.savgol_polyorder: int = 2
        # Numero de amostras para a média espectral
        self.mean_samples: int = 10

        # Thread e worker para aquisição de dados
        self.thread: QThread | None = None
        self.worker: DataAcquisition | None = None
        #Timer para solicitar dados periodicamente
        self.timer: QTimer | None = None
        self._cycle_started_at: float | None = None
        self._cycle_pending_responses: int = 0
        self._cycle_channels: list[int] = []
        self._cycle_channel_index: int = 0
        # Flag para evitar chamadas concorrentes de _cleanup_thread
        self._is_stopping = False
        
        # Instância compartilhada de PyCCT/OSA para evitar conflito de múltiplas instâncias
        self.osa = None
        # Marca que a última falha foi de comunicação e o handle compartilhado precisa ser recriado
        self._connection_lost = False
        # Configurações persistentes da janela de análise
        self.settings = QSettings('LiTel', 'online_process_timeseries')

        self.setup_connections()
        self.setup_plot()
        self._load_persistent_settings()

    def setup_plot(self):
        """
        Configura os widgets de gráfico da pyqtgraph.
        
        """
        # --- Configuração do gráfico: Espectro ---
        self.spectraPlotWidget.setLabel('left', 'Potência', units='dBm')
        self.spectraPlotWidget.showGrid(x=True, y=True)
        xAxis = pg.AxisItem(orientation='bottom')
        xAxis.setLabel(text='Comprimento de Onda', units='nm')
        xAxis.enableAutoSIPrefix(False) # Mantém unidades em nm
        self.spectraPlotWidget.setAxisItems({'bottom': xAxis})

        # Adiciona a região de seleção (ROI)
        self.spectraPlotWidget.addItem(self.roi_region)

        # --- Configuração do gráfico: Evolução Temporal ---
        self.temporalPlotWidget.setLabel('bottom', 'Timestamp')
        self.temporalPlotWidget.showGrid(x=False, y=True)
        self.temporalPlotWidget.setAxisItems({'bottom': pg.DateAxisItem()})
        yAxis = pg.AxisItem(orientation='left')
        yAxis.setLabel(text='Comprimento de Onda', units='nm')
        yAxis.enableAutoSIPrefix(False) # Mantém unidades em nm
        self.temporalPlotWidget.setAxisItems({'left': yAxis})

        # --- Configuração do gráfico: Box Plot ---
        self.boxPlot = self.boxPlotWidget.addPlot()
        self.boxLegend = self.boxPlot.addLegend()
        self.boxPlot.setXRange(0, 2)
        self.boxPlot.getAxis('bottom').setTicks([]) # Remove os números do eixo X

    def setup_connections(self):
        """
        Conecta os sinais dos widgets (eventos) aos seus respectivos slots (métodos).

        """
        self.stop_btn.clicked.connect(self.toggle_thread) # Inicia/Para a aquisição de dados
        self.save_btn.clicked.connect(self.save_data) # Salva os dados processados
        self.clear_btn.clicked.connect(self.clear_plot) # Limpa os gráficos
        self.window_btn.clicked.connect(self.select_window_method)
        self.savgol_btn.clicked.connect(self.select_savgol_parameters)
        self.mean_btn.clicked.connect(self.select_mean_samples)
        self.actionPeaks.triggered.connect(self.select_peak_parameters)
        self.temporal_roi_region.sigRegionChanged.connect(self.roi_changed) # Atualiza o box plot mesmo com a aquisição parada
        self.roi_region.sigRegionChanged.connect(self._spectrum_roi_changed)
        
        # Botões de fixar um espectro
        for button in self._list_fix_buttons():
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda pos, btn=button: self.toggle_fix(btn, pos))
            button.clicked.connect(lambda b, btn=button: self.fix_btn_clicked(btn, btn.isChecked()))

        self.actionM.triggered.connect(lambda: self.unit_changed('m'))
        self.actionUm.triggered.connect(lambda: self.unit_changed('um'))
        self.actionNm.triggered.connect(lambda: self.unit_changed('nm'))
        self.actionPm.triggered.connect(lambda: self.unit_changed('pm'))
        self.actionLight.triggered.connect(lambda: self.set_theme('light'))
        self.actionDark.triggered.connect(lambda: self.set_theme('dark'))
        self.actionHelp.triggered.connect(lambda: webbrowser.open(
            'https://github.com/pedroproprio/online_process_timeseries/'))
        self.actionImon.triggered.connect(lambda: webbrowser.open(
            'https://ftdichip.com/drivers/'))
        self.actionSwitch.triggered.connect(lambda: webbrowser.open(
            'https://www.silabs.com/software-and-tools/usb-to-uart-bridge-vcp-drivers?tab=downloads'))
        self.actionOpenFile.triggered.connect(self.open_file)
        self.actionNewWindow.triggered.connect(self.open_new_window)

        # Toggle
        self.continuous_chk = ToggleSwitch(self.continuous_hlay)

        # Ícones dos botões
        main_dir = os.path.dirname(os.path.abspath(argv[0]))
        apo = os.path.join(main_dir, "img", "apodization.png")
        self.window_btn.setIcon(QIcon(apo))
        svg = os.path.join(main_dir, "img", "savgol.png")
        self.savgol_btn.setIcon(QIcon(svg))
        mean = os.path.join(main_dir, "img", "mean.png")
        self.mean_btn.setIcon(QIcon(mean))

        # Conecta atalhos do teclado
        self.actionOpenFile.setShortcut('Ctrl+A')
        self.actionNewWindow.setShortcut('Ctrl+N')
        self.actionPeaks.setShortcut('Ctrl+E')
        self.stop_btn.setShortcut('Return')
        self.clear_btn.setShortcut('Ctrl+L')
        self.save_btn.setShortcut('Ctrl+S')
        self.window_btn.setShortcut('J')
        self.savgol_btn.setShortcut('S')
        self.mean_btn.setShortcut('M')
        for i, button in enumerate(self._list_fix_buttons()):
            button.setShortcut(f'{i+1}') # Atalhos 1-6 para fixar espectros

        # Tabs
        self.tabWidget.currentChanged.connect(self._on_tab_changed)

    def _load_persistent_settings(self):
        """
        Carrega preferências persistidas globais da janela de análise.
        As configurações específicas de fibra (window, savgol, mean, peaks) são
        carregadas separadamente em _load_fiber_specific_settings().

        """
        saved_theme = self.settings.value('analysis/theme', self.theme, type=str)
        self.theme = saved_theme if saved_theme in ('light', 'dark') else 'dark'

        saved_unit = self.settings.value('analysis/x_unit', self.xUnit, type=str)
        self.xUnit = saved_unit if saved_unit in ('m', 'um', 'nm', 'pm') else 'nm'
        self.actionM.setChecked(self.xUnit == 'm')
        self.actionUm.setChecked(self.xUnit == 'um')
        self.actionNm.setChecked(self.xUnit == 'nm')
        self.actionPm.setChecked(self.xUnit == 'pm')
        self._set_spectrum_axis_unit()

    def _save_persistent_settings(self):
        """
        Salva preferências persistentes da janela de análise.
        Configurações específicas de fibra são salvas com prefixo de fibra.

        """
        # Salva configurações específicas de fibra (window, savgol, mean, peaks)
        self._save_fiber_specific_settings()

        key_sample_rate = self._interface_settings_key('sample_rate')
        if key_sample_rate:
            self.settings.setValue(key_sample_rate, int(self.sr_spin.value()))

        theme_value = self.theme
        if isinstance(self.config_data, dict):
            theme_value = self.config_data.get('theme', theme_value)
        if theme_value not in ('light', 'dark'):
            theme_value = 'dark'
        self.settings.setValue('analysis/theme', theme_value)

        x_unit_value = self.xUnit if self.xUnit in ('m', 'um', 'nm', 'pm') else 'nm'
        self.settings.setValue('analysis/x_unit', x_unit_value)
        self._save_roi_for_current_mode()
        self.settings.sync()

    def _analysis_mode_key(self, key_name: str) -> str | None:
        """
        Retorna a chave de settings específica de interface+fibra.
        Args:
            key_name (str): Nome da chave base (ex: 'window', 'savgol_window_points').
        Returns:
            str | None: Chave com prefixo de interface e fibra, ou None se faltar contexto.

        """
        if not isinstance(self.config_data, dict):
            return None

        inter = str(self.config_data.get('inter', '')).strip()
        fiber = self._fiber_mode().strip()
        if not inter or not fiber:
            return None

        inter_key = inter.replace('/', '_').replace(' ', '_')
        fiber_key = fiber.replace('/', '_').replace(' ', '_')
        return f'analysis/{inter_key}/{fiber_key}/{key_name}'

    def _interface_settings_key(self, key_name: str) -> str | None:
        """
        Retorna a chave de settings específica de interface.

        """
        if not isinstance(self.config_data, dict):
            return None

        inter = str(self.config_data.get('inter', '')).strip()
        if not inter:
            return None

        inter_key = inter.replace('/', '_').replace(' ', '_')
        return f'analysis/{inter_key}/{key_name}'

    def _get_default_window(self) -> str | None:
        """
        Retorna o valor padrão de window (apodização) baseado no instrumento.
        """
        # Todos os instrumentos usam None como padrão (sem apodização)
        return None

    def _roi_settings_key(self) -> str | None:
        """
        Retorna a chave de settings da ROI por interface e tipo de fibra.

        """
        if not isinstance(self.config_data, dict):
            return None

        inter = str(self.config_data.get('inter', '')).strip()
        fiber = str(self.config_data.get('fiber', '')).strip()
        if not inter or not fiber:
            return None

        inter_key = inter.replace('/', '_')
        fiber_key = fiber.replace('/', '_')
        return f'analysis/roi_range/{inter_key}/{fiber_key}'

    def _save_fiber_specific_settings(self):
        """
        Salva as configurações específicas de interface+fibra (window, savgol, mean, peaks).
        Estas configurações variam por tipo de interface e de fibra.
        
        """
        # Window (apodização)
        key_window = self._analysis_mode_key('window')
        if key_window:
            self.settings.setValue(key_window, self.window or 'None')
        
        # Savitzky-Golay
        key_savgol_window = self._analysis_mode_key('savgol_window_points')
        key_savgol_poly = self._analysis_mode_key('savgol_polyorder')
        if key_savgol_window:
            self.settings.setValue(key_savgol_window, int(self.savgol_window_points))
        if key_savgol_poly:
            self.settings.setValue(key_savgol_poly, int(self.savgol_polyorder))
        
        # Mean samples
        key_mean = self._analysis_mode_key('mean_samples')
        if key_mean:
            self.settings.setValue(key_mean, int(self.mean_samples))
        
        # Peak detection params — salva por fibra, com fallback para interface
        key_prominence = self._analysis_mode_key('peak_prominence') or self._interface_settings_key('peak_prominence')
        key_width = self._analysis_mode_key('peak_width') or self._interface_settings_key('peak_width')
        key_distance = self._analysis_mode_key('peak_distance') or self._interface_settings_key('peak_distance')
        if key_prominence:
            self.settings.setValue(key_prominence, float(self.peak_detection_params['prominence']))
        if key_width:
            self.settings.setValue(key_width, float(self.peak_detection_params['width']))
        if key_distance:
            self.settings.setValue(key_distance, int(self.peak_detection_params['distance']))

    def _load_fiber_specific_settings(self):
        """
        Carrega as configurações específicas de interface+fibra (window, savgol, mean, peaks).
        Chamado em load_config() após config_data estar definido.
        
        """
        # Window (apodização)
        key_window = self._analysis_mode_key('window')
        if key_window:
            default_window = self._get_default_window()
            # Usa 'None' como chave de busca para compatibilidade com QSettings
            saved_window = self.settings.value(key_window, default_window or 'None', type=str)
            if saved_window in self.window_methods:
                self.window = saved_window
            else:
                self.window = default_window
        
        # Savitzky-Golay
        key_savgol_window = self._analysis_mode_key('savgol_window_points')
        key_savgol_poly = self._analysis_mode_key('savgol_polyorder')
        if key_savgol_window and key_savgol_poly:
            saved_window = self.settings.value(key_savgol_window, self.savgol_window_points, type=int)
            saved_poly = self.settings.value(key_savgol_poly, self.savgol_polyorder, type=int)
            window_points = max(0, int(saved_window))
            if window_points % 2 == 0 and window_points > 0:
                window_points -= 1
            polyorder = max(1, int(saved_poly))
            if window_points > 0 and polyorder >= window_points:
                polyorder = max(1, window_points - 1)
            self.savgol_window_points = window_points
            self.savgol_polyorder = polyorder
        
        # Mean samples
        # Usa padrão específico do instrumento se não houver valor salvo
        key_mean = self._analysis_mode_key('mean_samples')
        if key_mean:
            if isinstance(self.config_data, dict):
                inter = str(self.config_data.get('inter', '')).strip()
                if inter == 'THORLABS OSA203':
                    self.mean_samples = 1
            saved_mean = self.settings.value(key_mean, self.mean_samples, type=int)
            self.mean_samples = int(saved_mean)
        
        # Peak detection params (carrega cada chave independentemente)
        key_prominence = self._analysis_mode_key('peak_prominence') or self._interface_settings_key('peak_prominence')
        key_width = self._analysis_mode_key('peak_width') or self._interface_settings_key('peak_width')
        key_distance = self._analysis_mode_key('peak_distance') or self._interface_settings_key('peak_distance')
        if key_prominence:
            saved_prominence = self.settings.value(
                key_prominence,
                self.peak_detection_params['prominence'],
                type=float,
            )
            try:
                self.peak_detection_params['prominence'] = max(0.0, float(saved_prominence))
            except Exception:
                pass
        if key_width:
            saved_width = self.settings.value(
                key_width,
                self.peak_detection_params['width'],
                type=float,
            )
            try:
                self.peak_detection_params['width'] = max(0.0, float(saved_width))
            except Exception:
                pass
        if key_distance:
            saved_distance = self.settings.value(
                key_distance,
                self.peak_detection_params['distance'],
                type=int,
            )
            try:
                self.peak_detection_params['distance'] = max(1, int(saved_distance))
            except Exception:
                pass

    def _load_interface_specific_settings(self):
        """
        Carrega as configurações específicas de interface, como sample_rate.

        """
        key_sample_rate = self._interface_settings_key('sample_rate')
        if key_sample_rate:
            saved_sample_rate = self.settings.value(key_sample_rate, self.sr_spin.value(), type=int)
            self.sr_spin.setValue(int(saved_sample_rate))
            self.sample_rate = int(self.sr_spin.value())

    def _save_roi_for_current_mode(self):
        """
        Salva a ROI espectral atual para a combinação interface+fibra.
        A persistência é feita em metros para manter consistência entre unidades.

        """
        key = self._roi_settings_key()
        if key is None or self.roi_range is None:
            return

        unit_to_m = self._unit_to_meter_factor(self.xUnit)
        roi_min_m = float(self.roi_range[0]) * unit_to_m
        roi_max_m = float(self.roi_range[1]) * unit_to_m
        if roi_max_m <= roi_min_m:
            return

        self.settings.setValue(key, f'{roi_min_m},{roi_max_m}')

    def _restore_roi_for_current_mode(self):
        """
        Restaura a ROI espectral salva para a combinação interface+fibra.
        Converte de metros para a unidade de eixo X atualmente ativa.

        """
        default_roi = self._default_roi_for_current_mode()
        key = self._roi_settings_key()
        if key is None:
            if default_roi is not None:
                self.roi_range = default_roi
                self.roi_region.setRegion(self.roi_range)
                # Aplica ROI padrão a todos os canais habilitados
                if hasattr(self, 'enabled_channels') and isinstance(self.enabled_channels, list):
                    for idx in self.enabled_channels:
                        state = self.channel_states.setdefault(idx, self._default_channel_state())
                        state['roi_range'] = list(self.roi_range)
            return

        value = self.settings.value(key, '', type=str)
        if not value:
            if default_roi is not None:
                self.roi_range = default_roi
                self.roi_region.setRegion(self.roi_range)
                # Aplica ROI padrão a todos os canais habilitados
                if hasattr(self, 'enabled_channels') and isinstance(self.enabled_channels, list):
                    for idx in self.enabled_channels:
                        state = self.channel_states.setdefault(idx, self._default_channel_state())
                        state['roi_range'] = list(self.roi_range)
            return

        try:
            roi_min_m_str, roi_max_m_str = [part.strip() for part in value.split(',')]
            roi_min_m = float(roi_min_m_str)
            roi_max_m = float(roi_max_m_str)
        except Exception:
            if default_roi is not None:
                self.roi_range = default_roi
                self.roi_region.setRegion(self.roi_range)
            return

        if roi_max_m <= roi_min_m:
            if default_roi is not None:
                self.roi_range = default_roi
                self.roi_region.setRegion(self.roi_range)
                # Aplica ROI padrão a todos os canais habilitados
                if hasattr(self, 'enabled_channels') and isinstance(self.enabled_channels, list):
                    for idx in self.enabled_channels:
                        state = self.channel_states.setdefault(idx, self._default_channel_state())
                        state['roi_range'] = list(self.roi_range)
            return

        m_to_unit = 1.0 / self._unit_to_meter_factor(self.xUnit)
        self.roi_range = [roi_min_m * m_to_unit, roi_max_m * m_to_unit]
        self.roi_region.setRegion(self.roi_range)
        # Aplica ROI restaurada a todos os canais habilitados
        if hasattr(self, 'enabled_channels') and isinstance(self.enabled_channels, list):
            for idx in self.enabled_channels:
                state = self.channel_states.setdefault(idx, self._default_channel_state())
                state['roi_range'] = list(self.roi_range)

    def _default_roi_for_current_mode(self) -> list[float] | None:
        """
        Retorna a ROI padrão (25%-75% do range configurado) para o modo atual.
        Essa regra replica o padrão aplicado em update_plot quando não há ROI.

        """
        if not isinstance(self.config_data, dict):
            return None

        range_cfg = self.config_data.get('range')
        if not range_cfg or len(range_cfg) != 2:
            return None

        x_min_m = float(min(range_cfg[0], range_cfg[1]))
        x_max_m = float(max(range_cfg[0], range_cfg[1]))
        if x_max_m <= x_min_m:
            return None

        m_to_unit = 1.0 / self._unit_to_meter_factor(self.xUnit)
        x_min = x_min_m * m_to_unit
        x_max = x_max_m * m_to_unit
        x_range = x_max - x_min
        return [x_min + 0.25 * x_range, x_max - 0.25 * x_range]

    def _empty_result_store(self) -> dict:
        """
        Returns: 
            dict: um dicionário vazio para armazenar os resultados processados, com chaves pré-definidas.
        
        """
        return {'Timestamp': [], 'Intensidade': [], 'Vale': [], 'Picos': []}

    def _fiber_mode(self) -> str:
        """
        Returns:
            str: Modo normalizado da fibra. Retorna 'FBG', 'INT' ou o valor original.

        """
        fiber = str(self.config_data.get('fiber', '')).strip()
        fiber_key = fiber.casefold()
        if fiber_key == 'fbg':
            return 'FBG'
        if fiber_key in ('int', 'interferômetro', 'interferometro', 'interferometer'):
            return 'INT'
        return fiber

    def _default_trace_state(self, trace_number: int) -> dict:
        """Retorna o estado padrão de um trace do BraggMeter."""
        return {
            'spectra_data': None,
            'roi_range': None,
            'temporal_roi_range': None,
            'results_df': self._empty_result_store(),
            'samples': {},
            'pending_hdf5': self._empty_result_store(),
            'fixed_traces': {},
            'active_traces': [],
            'error_messages': [],
            'warnings': [],
            'add_legend': True,
            'in_view_trace': trace_number,
        }

    def _is_braggmeter(self) -> bool:
        """ 
        Returns:
            bool: True se a interface for um BraggMeter, False caso contrário.

        """
        inter = self.config_data.get('inter')
        return 'BRAGGMETER' in inter

    def _active_bragg_traces(self) -> list[int]:
        """
        Returns:
            list[int]: Lista de índices dos traces ativos do BraggMeter.
        
        """
        trace = []
        for i, t in enumerate(self.config_data.get('bragg_traces')):
            if t:
                trace.append(i)
        return trace

    def _apply_state_snapshot(self, state: dict, sync_regions: bool = True):
        """Aplica um snapshot de estado aos atributos da janela sem trocar de aba."""
        self.spectra_data = state['spectra_data']
        self.roi_range = state['roi_range']
        temporal_roi_range = state.get('temporal_roi_range')
        self.results_df = state['results_df']
        self.results_df.setdefault('Vale', [])
        self.results_df.setdefault('Picos', [])
        self.samples = state['samples']
        self.pending_hdf5 = state['pending_hdf5']
        self.pending_hdf5.setdefault('Vale', [])
        self.pending_hdf5.setdefault('Picos', [])
        self.fixed_traces = state['fixed_traces']
        self.active_traces = state['active_traces']
        self.error_messages = state['error_messages']
        self.in_view_trace = state.get('in_view_trace')

        if self.roi_range is not None:
            if sync_regions:
                self.roi_region.setRegion(self.roi_range)
            else:
                self.roi_region.blockSignals(True)
                try:
                    self.roi_region.setRegion(self.roi_range)
                finally:
                    self.roi_region.blockSignals(False)

        if temporal_roi_range is not None:
            if sync_regions:
                self.temporal_roi_region.setRegion(temporal_roi_range)
            else:
                self.temporal_roi_region.blockSignals(True)
                try:
                    self.temporal_roi_region.setRegion(temporal_roi_range)
                finally:
                    self.temporal_roi_region.blockSignals(False)

    def _result_key(self) -> str:
        """
        Returns:
            str: a chave correta para armazenar os resultados processados, dependendo do tipo de fibra selecionada.
        
        """
        return 'Picos' if self._fiber_mode() in ('FBG', 'INT') else 'Vale'

    def _flatten_peak_values(self, values) -> list[float]:
        """
        Args:
            values: uma lista que pode conter valores numéricos únicos ou arrays de picos.
        Returns:
            list[float]: uma lista "achatada" de valores numéricos, convertendo arrays e valores únicos para float.
        
        """
        flattened: list[float] = []
        for item in values or []:
            array = np.asarray(item, dtype=float)
            if array.ndim == 0:
                flattened.append(float(array))
            else:
                flattened.extend(array.ravel().tolist())
        return flattened

    def _peak_series_colors(self, count: int):
        """
        Args:
            count (int): o número de séries de picos distintas a serem coloridas.
        Returns:
            list[pg.mkColor]: uma lista de cores distintas para cada série de picos.
        
        """
        if count <= 0:
            return []
        hues = max(count, 3)
        return [pg.intColor(index, hues=hues) for index in range(count)]

    def _fbg_peak_match_tolerance_m(self) -> float:
        """
        Returns:
            float: a tolerância (em metros) para associar picos FBG entre medições.
        
        """
        res_m = self.config_data.get('res') if self.config_data else None
        if res_m is None:
            return 0.0
        return float(self.peak_detection_params['distance']) * float(res_m)

    def _find_matching_peak_key(self, peak_map: dict, wavelength: float) -> float | None:
        """
        Busca uma chave existente no mapa de picos dentro da tolerância configurada.
        Args:
            peak_map (dict): Dicionário onde as chaves são os wavelengths de referência dos picos.
            wavelength (float): O comprimento de onda do pico a ser associado.
        Returns:
            float | None: O comprimento de onda da chave correspondente encontrada, ou None se não houver correspondência dentro da tolerância.
            
        """
        tolerance = self._fbg_peak_match_tolerance_m()
        for existing_wl in peak_map.keys():
            if abs(existing_wl - wavelength) <= tolerance:
                return existing_wl
        return None

    def _group_fbg_peak_series(self, timestamps: list[float], peak_series: list[list[float]]) -> dict[float, list[tuple[float, float]]]:
        """
        Agrupa picos FBG por recorrência de comprimento de onda.
        Args:
            - timestamps: Lista de timestamps das aquisições.
            - peak_series: Lista de listas de picos detectados em cada aquisição.
        Returns:
            dict: {wavelength_referencia: [(timestamp, wavelength), ...]} ordenado por wavelength.
        
        """
        grouped: dict[float, list[tuple[float, float]]] = {}

        for timestamp, peaks in zip(timestamps, peak_series):
            for peak_wavelength in sorted(peaks):
                peak_wavelength = float(peak_wavelength)
                match_key = self._find_matching_peak_key(grouped, peak_wavelength)
                if match_key is None:
                    match_key = peak_wavelength
                    grouped[match_key] = []
                grouped[match_key].append((float(timestamp), peak_wavelength))

        return dict(sorted(grouped.items(), key=lambda item: item[0]))

    def _is_temporally_consistent_peak_group(
        self,
        group_points: list[tuple[float, float]],
        all_timestamps: list[float],
        min_relative_recurrence: float = 0.25,
        max_allowed_jump_factor: float = 2.5,
        max_irregular_jump_ratio: float = 0.35,
        expected_jump: float | None = None,
    ) -> bool:
        """
        Valida se um grupo de pico é recorrente e temporalmente consistente.

        Critérios:
        - Recorrência relativa mínima no histórico (presença em fração das aquisições);
        - Saltos temporais entre ocorrências não podem ser majoritariamente irregulares.
        Args:
            - group_points: Lista de tuplas (timestamp, wavelength) para um grupo de pico específico.
            - all_timestamps: Lista de timestamps das aquisições.
            - min_relative_recurrence: Recorrência relativa mínima.
            - max_allowed_jump_factor: Fator máximo de salto permitido.
            - max_irregular_jump_ratio: Proporção máxima de saltos irregulares.
            - expected_jump: Mediana dos saltos temporais globais pré-calculada pelo chamador.
              Quando fornecida, evita recalcular np.diff(all_timestamps) para cada grupo.
        Returns:
            bool: True se o grupo for temporalmente consistente, False caso contrário.

        """
        total_samples = len(all_timestamps)
        if total_samples == 0:
            return False

        group_count = len(group_points)
        relative_recurrence = group_count / total_samples
        if relative_recurrence < min_relative_recurrence:
            return False

        # Para grupos muito curtos, a recorrência relativa já é o principal critério.
        if group_count < 3:
            return True

        # Usa o expected_jump pré-calculado quando disponível; caso contrário deriva dos timestamps.
        if expected_jump is None:
            ts_all = np.asarray(all_timestamps, dtype=float)
            global_jumps = np.diff(ts_all)
            global_jumps = global_jumps[global_jumps > 0]
            if global_jumps.size == 0:
                return True
            expected_jump = float(np.median(global_jumps))

        if expected_jump <= 0:
            return True

        ts_group = np.asarray([point[0] for point in group_points], dtype=float)
        ts_group.sort()
        group_jumps = np.diff(ts_group)
        group_jumps = group_jumps[group_jumps > 0]
        if group_jumps.size == 0:
            return True

        jump_factor = group_jumps / expected_jump
        irregular_jumps = jump_factor > max_allowed_jump_factor
        irregular_ratio = float(np.mean(irregular_jumps))

        return irregular_ratio <= max_irregular_jump_ratio

    def _recurring_fbg_peak_groups(self, timestamps: list[float], peak_series: list[list[float]], min_count: int = 2) -> dict[float, list[tuple[float, float]]]:
        """
        Filtra grupos de picos FBG recorrentes e temporalmente consistentes.
        Args:
            - timestamps: Lista de timestamps das aquisições.
            - peak_series: Lista de listas de picos detectados em cada aquisição.
            - min_count: Número mínimo de ocorrências para um grupo ser considerado recorrente.
        Returns:
            dict: {wavelength_referencia: [(timestamp, wavelength), ...]}

        """
        # Limita o histórico analisado ao teto de memória do results_df para manter
        # _group_fbg_peak_series em O(max_live_points) independentemente da duração da sessão.
        limit = self.max_live_points
        if len(timestamps) > limit:
            timestamps  = timestamps[-limit:]
            peak_series = peak_series[-limit:]

        grouped = self._group_fbg_peak_series(timestamps, peak_series)

        # Calcula expected_jump uma única vez para todos os grupos, evitando refazer
        # np.diff(all_timestamps) dentro de _is_temporally_consistent_peak_group a cada grupo.
        if timestamps:
            ts_all = np.asarray(timestamps, dtype=float)
            global_jumps = np.diff(ts_all)
            global_jumps = global_jumps[global_jumps > 0]
            expected_jump: float | None = float(np.median(global_jumps)) if global_jumps.size > 0 else None
        else:
            expected_jump = None

        filtered = {}
        for wl, points in grouped.items():
            if len(points) < min_count:
                continue
            if not self._is_temporally_consistent_peak_group(points, timestamps, expected_jump=expected_jump):
                continue
            filtered[wl] = points
        return filtered

    def _fbg_current_peak_samples(
        self,
        timestamps: list[float],
        peak_series: list[list[float]],
        label_prefix: str = "Atual",
        feature_label: str = "Pico",
    ) -> list[tuple[str, tuple]]:
        """
        Retorna box plots por pico FBG recorrente dentro da ROI temporal ativa.
        """
        recurring_groups = self._recurring_fbg_peak_groups(timestamps, peak_series, min_count=2)
        if not recurring_groups:
            return []

        roi_min_ts, roi_max_ts = self.temporal_roi_region.getRegion()
        peak_samples: list[tuple[str, tuple]] = []

        for i, (peak_wavelength, points) in enumerate(recurring_groups.items(), start=1):
            roi_values_m = [wl for ts, wl in points if roi_min_ts <= ts <= roi_max_ts]
            if len(roi_values_m) == 0:
                continue
            peak_values_unit = self._from_meter(roi_values_m, self.resultUnit)
            peak_samples.append((f"{label_prefix} - {feature_label} {i}", self.box_plot_statistics(peak_values_unit)))

        return peak_samples

    def _expand_samples_for_boxplot(self) -> list[tuple[str, tuple]]:
        """Expande amostras salvas para o formato linear esperado pelo plot de box."""
        expanded: list[tuple[str, tuple]] = []
        for sample_name, sample_data in self.samples.items():
            if isinstance(sample_data, list):
                for sub_label, stats in sample_data:
                    expanded.append((f"{sample_name} - {sub_label}", stats))
            else:
                expanded.append((sample_name, sample_data))
        return expanded

    def _get_fbg_peak_color(self, wavelength: float, used_keys_in_spectrum: set | None = None) -> tuple:
        """
        Obtém ou atribui uma cor consistente para um pico FBG baseado em seu wavelength.
        Args:
            wavelength (float): Comprimento de onda em metros.
        Returns:
            tuple: Cor (QColor ou valor propto para pyqtgraph).

        """
        # Procura por um pico existente próximo a este wavelength
        matching_wl = self._find_matching_peak_key(self.fbg_peak_color_map, wavelength)

        # Se encontrou um match e essa chave ainda não foi usada no espectro
        if matching_wl is not None:
            if used_keys_in_spectrum is None or matching_wl not in used_keys_in_spectrum:
                if used_keys_in_spectrum is not None:
                    used_keys_in_spectrum.add(matching_wl)
                return self.fbg_peak_color_map[matching_wl]

        # Caso contrário — ou não encontrou match, ou a chave já foi usada
        # — cria um novo mapeamento persistente para este wavelength e retorna.
        # Antes de inserir, descarta a entrada mais antiga se o mapa estiver cheio;
        # impede crescimento ilimitado causado por drift de picos ao longo de horas.
        if len(self.fbg_peak_color_map) >= self._MAX_PEAK_COLORS:
            oldest_key = next(iter(self.fbg_peak_color_map))
            del self.fbg_peak_color_map[oldest_key]
        peak_count = len(self.fbg_peak_color_map)
        max_peaks = max(peak_count + 1, 3)
        new_color = pg.intColor(peak_count, hues=max_peaks)
        self.fbg_peak_color_map[wavelength] = new_color
        if used_keys_in_spectrum is not None:
            used_keys_in_spectrum.add(wavelength)
        return new_color

    def _clear_fbg_peak_colors(self):
        """
        Limpa o mapa de cores dos picos FBG (útil para reset de análise).
        
        """
        self.fbg_peak_color_map.clear()

    def _apply_fiber_mode(self):
        """
        Aplica as configurações específicas para o tipo de fibra selecionado.
        
        """
        fiber = self._fiber_mode()
        is_fbg = fiber == 'FBG'
        is_peak_mode = fiber in ('FBG', 'INT')
        self.actionPeaks.setEnabled(is_peak_mode)
        w = 'picos' if is_fbg else 'vales'
        self.actionPeaks.setText(f'Encontrar {w}')
        self.roi_region.setVisible(not is_fbg)
        self.temporal_roi_region.setVisible(True)
        if is_peak_mode:
            self.roi_range = None
            self._clear_fbg_peak_colors()  # Limpa mapa de cores ao mudar para modo com séries recorrentes

    def _style_merge_plot_specs(self, plot_specs: list[tuple[object, object | None]]):
        """Aplica a paleta atual a um grupo de widgets de merge."""
        for plot_widget, _ in plot_specs:
            if plot_widget is None:
                continue
            plot_widget.setBackground(self.theme_colors['plot_bg'])
            item = plot_widget.getPlotItem()
            item.getAxis('left').setPen(pg.mkPen(self.theme_colors['axis']))
            item.getAxis('left').setTextPen(pg.mkPen(self.theme_colors['axis']))
            item.getAxis('bottom').setPen(pg.mkPen(self.theme_colors['axis']))
            item.getAxis('bottom').setTextPen(pg.mkPen(self.theme_colors['axis']))

    def _set_merge_axis_unit_for_specs(self, plot_specs: list[tuple[object, object | None]]):
        """Atualiza os eixos X de um grupo de widgets de merge conforme a unidade atual."""
        unit_label = {
            'm': 'm',
            'um': 'μm',
            'nm': 'nm',
            'pm': 'pm',
        }.get(self.xUnit, 'nm')

        for idx, (plot_widget, _) in enumerate(plot_specs):
            if plot_widget is None:
                continue
            if idx == 0:
                plot_widget.setLabel('left', 'Potência', units='dBm')
            else:
                plot_widget.setLabel('left', '')
            plot_widget.showGrid(x=(idx == 0), y=(idx == 0))
            bottom_axis = pg.AxisItem(orientation='bottom')
            if idx == 0:
                bottom_axis.setLabel(text='Comprimento de Onda', units=unit_label)
            else:
                bottom_axis.setLabel(text='')
            bottom_axis.enableAutoSIPrefix(False)
            plot_widget.setAxisItems({'bottom': bottom_axis})

    def _setup_merge_plots(self):
        """
        Configura os widgets da tab Merge com a mesma base visual dos demais widgets.

        """
        self.merge_plot_specs = [
            (self.mergePlotWidget, self.mergePlotWidget.addLegend()),
            (self.mergeCh1PlotWidget, None),
            (self.mergeCh2PlotWidget, None),
            (self.mergeCh3PlotWidget, None),
            (self.mergeCh4PlotWidget, None),
        ]

        self.bragg_merge_plot_specs = [
            (getattr(self, 'braggMergePlotWidget', None), getattr(self, 'braggMergePlotWidget', None).addLegend() if getattr(self, 'braggMergePlotWidget', None) is not None else None),
            (getattr(self, 'mergeT0PlotWidget', None), None),
            (getattr(self, 'mergeT1PlotWidget', None), None),
            (getattr(self, 'mergeT2PlotWidget', None), None),
            (getattr(self, 'mergeT3PlotWidget', None), None),
        ]

        self._set_merge_axis_unit_for_specs(self.merge_plot_specs)
        self._set_merge_axis_unit_for_specs(self.bragg_merge_plot_specs)

        self._style_merge_plot_specs(self.merge_plot_specs)
        self._style_merge_plot_specs(self.bragg_merge_plot_specs)

    def _refresh_bragg_merge_views(self):
        """Redesenha a aba Merge do Bragg usando apenas os traces ativos do canal atual."""
        if not hasattr(self, 'bragg_merge_plot_specs'):
            return

        for idx, (plot_widget, legend) in enumerate(self.bragg_merge_plot_specs):
            if plot_widget is None:
                continue
            plot_widget.clear()
            if legend is not None:
                legend.clear()
            if idx == 0:
                plot_widget.setLabel('left', 'Potência', units='dBm')
            else:
                plot_widget.setLabel('left', '')
            plot_widget.showGrid(x=(idx == 0), y=(idx == 0))

        active_channel_state = self.channel_states.get(self.active_channel_idx, {})
        trace_states = active_channel_state.get('trace_states', {}) or {}
        active_traces = [trace_number for trace_number in self._active_bragg_traces() if trace_number in trace_states]
        if not active_traces:
            return

        fix_button_colors = {
            str(button): button.palette().color(QPalette.ColorRole.Button)
            for button in self._list_fix_buttons()
        }
        trace_widgets = {
            trace_idx: widget
            for trace_idx, widget in enumerate(spec[0] for spec in self.bragg_merge_plot_specs[1:])
            if widget is not None
        }

        for trace_number in active_traces:
            trace_state = trace_states.get(trace_number, {})
            fixed_traces = trace_state.get('fixed_traces', {}) or {}
            if not fixed_traces:
                continue

            trace_widget = trace_widgets.get(trace_number)
            trace_color = self.merge_channel_colors[trace_number % len(self.merge_channel_colors)]
            trace_name = f'Trace {trace_number}'
            first_curve = True

            for trace_key, trace in fixed_traces.items():
                if trace is None:
                    continue

                x_vals = np.asarray(trace[0], dtype=float)
                y_vals = preprocess_plot_data(
                    np.asarray(trace[1], dtype=float),
                    self.window_methods,
                    self.savgol_window_points,
                    self.savgol_polyorder,
                    self.window,
                )

                self.braggMergePlotWidget.plot(
                    x_vals,
                    y_vals,
                    pen=pg.mkPen(trace_color, width=1),
                    name=trace_name if first_curve else None,
                )
                if trace_widget is not None:
                    trace_widget.plot(
                        x_vals,
                        y_vals,
                        pen=pg.mkPen(fix_button_colors.get(trace_key, trace_color), width=1),
                    )
                first_curve = False

    def _refresh_merge_views(self):
        """
        Redesenha a tab Merge com os espectros fixados salvos por canal.

        """
        if not hasattr(self, 'merge_plot_specs'):
            return

        for idx, (plot_widget, legend) in enumerate(self.merge_plot_specs):
            plot_widget.clear()
            if legend is not None:
                legend.clear()
            if idx == 0:
                plot_widget.setLabel('left', 'Potência', units='dBm')
            else:
                plot_widget.setLabel('left', '')
            plot_widget.showGrid(x=(idx==0), y=(idx==0))

        channel_widgets = [spec[0] for spec in self.merge_plot_specs[1:]]
        fix_button_colors = {
            str(button): button.palette().color(QPalette.ColorRole.Button)
            for button in self._list_fix_buttons()
        }

        for channel_idx in range(len(self.channel_states)):
            state = self.channel_states.get(channel_idx, {})
            trace_states = state.get('trace_states', {}) or {}
            if not trace_states:
                continue

            color = self.merge_channel_colors[channel_idx % len(self.merge_channel_colors)]
            channel_plot = channel_widgets[channel_idx]
            channel_name = f'Canal {channel_idx + 1}'

            first_channel_curve = True
            for trace_number, trace_state in sorted(trace_states.items()):
                fixed_traces = trace_state.get('fixed_traces', {}) or {}
                if not fixed_traces:
                    continue

                for trace_key, trace in fixed_traces.items():
                    if trace is None:
                        continue

                    x_vals = np.asarray(trace[0], dtype=float)
                    y_vals = preprocess_plot_data(
                        np.asarray(trace[1], dtype=float),
                        self.window_methods,
                        self.savgol_window_points,
                        self.savgol_polyorder,
                        self.window,
                    )

                    main_name = channel_name if first_channel_curve else None
                    self.mergePlotWidget.plot(x_vals, y_vals, pen=pg.mkPen(color, width=1), name=main_name)
                    trace_color = fix_button_colors.get(trace_key, color)
                    channel_plot.plot(x_vals, y_vals, pen=pg.mkPen(trace_color, width=1))
                    first_channel_curve = False

                self._refresh_bragg_merge_views()

    def _default_channel_state(self) -> dict:
        """
        Retorna o estado inicial padrão para um canal/aba, 
        usado para inicializar novos canais ou resetar canais existentes.
        Returns:
            dict: Dicionário com o estado inicial padrão para um canal/aba.

        """
        return {
            'spectra_data': None,
            'roi_range': None,
            'temporal_roi_range': None,
            'results_df': self._empty_result_store(),
            'samples': {},
            'pending_hdf5': self._empty_result_store(),
            'fixed_traces': {},
            'active_traces': [],
            'error_messages': [],
            'warnings': [],
            'add_legend': True,
            'in_view_trace': 0,
            'trace_states': {
                trace_number: self._default_trace_state(trace_number)
                for trace_number in range(len(self.config_data.get('bragg_traces')))
            },
        }

    def _clone_state_snapshot(self, state: dict) -> dict:
        """Create a shallow, container-safe copy of the current view state."""
        return {
            'spectra_data': list(state.get('spectra_data') or []),
            'roi_range': list(state['roi_range']) if state.get('roi_range') is not None else None,
            'temporal_roi_range': tuple(state['temporal_roi_range']) if state.get('temporal_roi_range') is not None else None,
            'results_df': {key: list(value) for key, value in state.get('results_df', {}).items()},
            'samples': dict(state.get('samples', {})),
            'pending_hdf5': {key: list(value) for key, value in state.get('pending_hdf5', {}).items()},
            'fixed_traces': dict(state.get('fixed_traces', {})),
            'active_traces': list(state.get('active_traces', [])),
            'error_messages': list(state.get('error_messages', [])),
            'warnings': list(state.get('warnings', [])),
            'in_view_trace': state.get('in_view_trace'),
        }

    def _capture_current_state(self, save_button_state: bool = True) -> dict:
        """Capture the active channel state as an isolated snapshot."""
        return self._clone_state_snapshot({
            'spectra_data': self.spectra_data or [],
            'roi_range': self.roi_range,
            'temporal_roi_range': self.temporal_roi_region.getRegion(),
            'results_df': self.results_df,
            'samples': self.samples,
            'pending_hdf5': self.pending_hdf5,
            'fixed_traces': self.fixed_traces,
            'active_traces': [str(btn) for btn in self._list_fix_buttons() if btn.isChecked()] if save_button_state else self.active_traces,
            'error_messages': self.error_messages,
            'in_view_trace': self._active_trace_number(),
        })

    def _enabled_tab_indices(self) -> list[int]:
        """
        Returns: 
            list[int]: Os índices das abas de canal atualmente habilitadas, ignorando a aba Merge.

        """
        merge_index = self.tabWidget.indexOf(getattr(self, 'tab_merge'))
        return [i for i in range(self.tabWidget.count()) if i != merge_index and self.tabWidget.isTabEnabled(i)]

    def _active_channel_number(self) -> int:
        """
        Returns:
            int: O número do canal ativo (1-4) com base no índice da aba ativa, ignorando a aba Merge.
        """
        return self.active_channel_idx + 1

    def _channel_index_from_number(self, channel: int) -> int:
        """
        Args:
            channel (int): Número do canal (1-4) para o qual se deseja obter o índice da aba.
        Returns:
            int: O índice da aba correspondente ao número do canal (1-4), ignorando a aba Merge.

        """
        merge_index = self.tabWidget.indexOf(getattr(self, 'tab_merge'))
        channel_tabs = [i for i in range(self.tabWidget.count()) if i != merge_index]
        return channel_tabs[channel - 1]

    def _active_trace_number(self) -> int | None:
        """Returns the currently visible Bragg trace number when applicable."""
        if not self._is_braggmeter():
            return None

        current_index = self.tab_bragg.currentIndex()
        if 0 <= current_index < self.tab_bragg.count():
            return current_index
        return None

    def _on_bragg_trace_changed(self, index: int):
        """Persist the previous trace and restore the newly selected one."""
        if not self._is_braggmeter():
            return

        previous_trace = self.in_view_trace
        if previous_trace is None:
            previous_trace = self._active_trace_number()
        if previous_trace is not None:
            self._save_active_channel_state(save_button_state=True, trace_number=previous_trace)

        if 0 <= index < self.tab_bragg.count():
            self._restore_channel_state(self.active_channel_idx, trace=index)
            # Ensure UI updates immediately when user switches trace tabs,
            # even if acquisition is paused.
            self._refresh_active_channel_view(sync_buttons=True)

    def _save_active_channel_state(self, save_button_state: bool = True, trace_number: int | None = None):
        """
        Salva o estado do canal ativo.
        Args:
            save_button_state (bool): Se True, salva o estado dos botões de fixação. Caso contrário, mantém o estado atual dos botões.
        
        """
        state = self._capture_current_state(save_button_state=save_button_state)
        channel_state = self.channel_states.setdefault(self.active_channel_idx, self._default_channel_state())
        visible_trace = self._active_trace_number() if self._is_braggmeter() else None
        if trace_number is None:
            trace_number = self.in_view_trace
        if trace_number is None and self._is_braggmeter():
            trace_number = self._active_trace_number()
        if trace_number is None:
            trace_number = 0

        if self._is_braggmeter() and isinstance(channel_state.get('trace_states'), dict):
            trace_state = channel_state['trace_states'].get(trace_number, {})
            state['warnings'] = list(trace_state.get('warnings', []))
            state['in_view_trace'] = trace_number
        else:
            state['warnings'] = list(channel_state.get('warnings', []))

        channel_state['in_view_trace'] = visible_trace if visible_trace is not None else trace_number
        channel_state['error_messages'] = state['error_messages']
        channel_state['trace_states'][trace_number] = self._clone_state_snapshot(state)

        if self._is_braggmeter() and isinstance(channel_state.get('trace_states'), dict):
            channel_state['trace_states'][trace_number] = self._clone_state_snapshot(state)
            channel_state['in_view_trace'] = visible_trace if visible_trace is not None else trace_number

    def _restore_channel_state(self, index: int, trace: int | None = None):
        """
        Restaura o estado de um canal específico.
        Args:
            index (int): Índice do canal/aba a ser restaurado.
        
        """
        state = self.channel_states.setdefault(index, self._default_channel_state())
        self.active_channel_idx = index

        trace_number = trace if trace is not None else state.get('in_view_trace', 0)
        if self._is_braggmeter() and isinstance(state.get('trace_states'), dict):
            trace_state = state['trace_states'].get(trace_number, state)
            state = trace_state
        else:
            trace_number = None

        self.spectra_data = state['spectra_data']
        self.roi_range = state['roi_range']
        temporal_roi_range = state.get('temporal_roi_range')
        self.results_df = state['results_df']
        self.results_df.setdefault('Vale', [])
        self.results_df.setdefault('Picos', [])
        self.samples = state['samples']
        self.pending_hdf5 = state['pending_hdf5']
        self.pending_hdf5.setdefault('Vale', [])
        self.pending_hdf5.setdefault('Picos', [])
        self.fixed_traces = state['fixed_traces']
        self.active_traces = state['active_traces']
        self.error_messages = state['error_messages']
        self.in_view_trace = trace_number if trace_number is not None else state.get('in_view_trace')

        if temporal_roi_range is not None:
            self.temporal_roi_region.setRegion(temporal_roi_range)

        if self._is_braggmeter() and hasattr(self, 'tab_bragg'):
            trace_index = trace_number if trace is None else trace
            if 0 <= trace_index < self.tab_bragg.count():
                target_widget = self.tab_bragg.widget(trace_index)
                if target_widget is not None:
                    current_widget = self.tab_bragg.currentWidget()
                    if current_widget is not target_widget:
                        self.tab_bragg.blockSignals(True)
                        self.tab_bragg.setCurrentIndex(trace_index)
                        self.tab_bragg.blockSignals(False)
                    layout = target_widget.layout()
                    if layout is None:
                        layout = QVBoxLayout(target_widget)
                    is_bragg_merge_tab = target_widget is getattr(self, 'bragg_merge', None)
                    if (not is_bragg_merge_tab) and self.main_qwt.parent() is not target_widget:
                        self.main_qwt.setParent(None)
                        layout.addWidget(self.main_qwt)

        self._sync_fix_buttons_ui()

    def _sync_fix_buttons_ui(self):
        """
        Sincroniza a interface dos botões de fixação com o estado atual.
        
        """
        for i, button in enumerate(self._list_fix_buttons()):
            has_trace = str(button) in self.fixed_traces
            checked = has_trace and str(button) in self.active_traces
            button.setChecked(checked)

            item = self.fixedLines_vlay.itemAt(i)
            line = item.widget()
            if has_trace:
                color = button.palette().color(QPalette.ColorRole.Button)
                line.setStyleSheet(
                    f"background-color: rgba({color.red()}, {color.green()}, {color.blue()}, 255)")
            else:
                line.setStyleSheet("background-color: transparent")

    def _refresh_active_channel_view(self, sync_buttons: bool = True):
        """
        Atualiza a visualização do canal ativo.
        Args:
            sync_buttons (bool): Se True, sincroniza o estado dos botões de fixação. Caso contrário, mantém o estado atual dos botões.
        
        """
        self.spectraPlotWidget.clear()
        self.spectraPlotWidget.addItem(self.roi_region)
        # spectraPlotWidget.clear() removeu as InfiniteLines; descarta as referências.
        self._peak_marker_lines.clear()
        self.temporalPlotWidget.clear()
        self.temporalPlotWidget.addItem(self.temporal_roi_region)
        self.boxPlot.clear()
        self.boxLegend.clear()

        if self.roi_range is not None:
            self.roi_region.setRegion(self.roi_range)

        temporal_roi_range = self.channel_states[self.active_channel_idx].get('temporal_roi_range')
        if temporal_roi_range is not None:
            self.temporal_roi_region.setRegion(temporal_roi_range)

        if sync_buttons:
            self._sync_fix_buttons_ui()

        if self.spectra_data:
            x, y = zip(*self.spectra_data)
            self._plot_spectrum_curve(np.asarray(x, dtype=float), np.asarray(y, dtype=float))

        if self.results_df['Timestamp']:
            self._update_plots_with_results()
        elif self.samples:
            self.update_box_plot()

        if self.file_path:
            self.setWindowTitle(f"Análise de dados - {os.path.basename(self.file_path)}")
        else:
            self.setWindowTitle("Análise de dados")

    def _request_cycle_data(self):
        """
        Solicita dados cíclicos da thread de aquisição para os canais habilitados.
        
        """
        if (not self._running) or self._is_stopping or self.worker is None or self.thread is None:
            return

        has_switch = bool(self.config_data.get('switch_ports'))

        channels = self.enabled_channels if has_switch else [self.active_channel_idx]
        if not channels:
            channels = [self.active_channel_idx]

        self._cycle_channels = list(channels)
        self._cycle_channel_index = 0
        self._cycle_started_at = time.monotonic()
        self._cycle_pending_responses = len(channels)
        self._request_next_cycle_channel()

    def _request_next_cycle_channel(self):
        """
        Dispara a próxima aquisição do ciclo de multiplexação.

        """
        if (not self._running) or self._is_stopping or self.worker is None or self.thread is None:
            return

        if self._cycle_channel_index >= len(self._cycle_channels):
            return

        ch_idx = self._cycle_channels[self._cycle_channel_index]
        self._cycle_channel_index += 1

        switch_channel = ch_idx + 1
        bragg_traces = self.config_data.get('bragg_traces')
        self.request_data_signal.emit(self.mean_samples, switch_channel, bragg_traces)

    def _arm_request_timer(self, delay_ms: int | None = None):
        """
        Rearma o timer de aquisição como disparo único.

        O próximo ciclo só é agendado depois que a aquisição atual termina,
        usando o intervalo definido apenas como tempo mínimo entre aquisições.

        """
        if self.timer is None:
            self.timer = QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self._request_cycle_data)

        if delay_ms is None:
            delay_ms = int(self.sample_rate)

        self.timer.start(delay_ms)

    def _on_cycle_response_received(self):
        """
        Conta respostas do ciclo atual e agenda o próximo disparo quando todas
        as leituras previstas terminarem.

        """
        if self._cycle_pending_responses <= 0:
            return

        self._cycle_pending_responses -= 1
        if self._cycle_pending_responses > 0:
            return

        if not self._running or self._is_stopping:
            return

        elapsed_ms = 0
        if self._cycle_started_at is not None:
            elapsed_ms = int((time.monotonic() - self._cycle_started_at) * 1000)

        next_delay_ms = max(0, int(self.sample_rate) - elapsed_ms)
        self._cycle_started_at = None
        self._arm_request_timer(next_delay_ms)

    def _handle_data_acquired(self, spec, warning, channel: int, *a):
        """
        Encaminha os dados recebidos e atualiza o agendamento do próximo ciclo.

        """
        try:
            self.update_plot(spec, warning, channel)
        finally:
            self._on_cycle_response_received()
            if (
                self._running
                and not self._is_stopping
                and self._cycle_pending_responses > 0
                and self._cycle_channel_index < len(self._cycle_channels)
            ):
                self._request_next_cycle_channel()

    def _on_tab_changed(self, index: int):
        """
        Event handler chamado quando o usuário muda de aba.
        Args:
            index (int): Índice da aba selecionada.

        """
        self._save_active_channel_state()
        self.main_qwt.setParent(None) # Remove o gráfico da hierarquia atual
        if self._is_braggmeter():
            self.tab_bragg.setParent(None)

        if index == self.tabWidget.indexOf(getattr(self, 'tab_merge')):
            self._refresh_merge_views()
            return

        self._restore_channel_state(index)
        self._refresh_merge_views()
        if self._is_braggmeter():
            self.tabWidget.widget(index).layout().addWidget(self.tab_bragg) # Adiciona as subabas do BraggMeter à aba selecionada
        else:
            self.tabWidget.widget(index).layout().addWidget(self.main_qwt) # Adiciona o container principal à aba selecionada
        self._refresh_active_channel_view(sync_buttons=False)

    def _unit_to_meter_factor(self, unit: str) -> float:
        """
        Args:
            unit (str): Unidade de comprimento ('m', 'um', 'nm', 'pm').
        Returns:
            float: o fator de conversão para converter valores para metros.

        """
        factors = {
            'm': 1.0,
            'um': 1e-6,
            'nm': 1e-9,
            'pm': 1e-12,
        }
        return factors.get(unit, 1e-9)

    def _set_spectrum_axis_unit(self):
        """
        Seleciona a unidade do eixo X do gráfico de espectro com base na configuração atual e atualiza o rótulo do eixo.

        """
        unit_label = {
            'm': 'm',
            'um': 'μm',
            'nm': 'nm',
            'pm': 'pm',
        }.get(self.xUnit, 'nm')
        self.spectraPlotWidget.setLabel('bottom', text='Comprimento de Onda', units=unit_label)

    def _from_meter(self, values, unit: str):
        """
        Returns:
            float: O fator de conversão para converter de metros para a unidade especificada.

        """
        return np.asarray(values, dtype=float) / self._unit_to_meter_factor(unit)

    def select_window_method(self):
        """
        Abre um diálogo para selecionar o método de janela a ser aplicado no espectro, ou 'None' para desabilitar a janela.

        """
        items = [*self.window_methods.keys(), 'None']
        current_item = self.window if self.window is not None else 'None'
        current_index = items.index(current_item)

        selected, accepted = QInputDialog.getItem(
            self,
            'Janela',
            'Selecione o método:',
            items,
            current_index,
            False,
        )
        if not accepted:
            return

        self.window = None if selected == 'None' else selected
        logger.info(f"Método de janela selecionado: {self.window or 'None'}")
        self._save_persistent_settings()

        if self.spectra_data:
            x, y = zip(*self.spectra_data)
            self._plot_spectrum_curve(np.asarray(x, dtype=float), np.asarray(y, dtype=float))

    def select_savgol_parameters(self):
        """
        Abre um diálogo para configurar os parâmetros do filtro Savitzky-Golay (tamanho da janela e grau do polinômio).

        """
        dialog = QDialog(self)
        dialog.setWindowTitle('Filtro Savitzky-Golay')

        layout = QFormLayout(dialog)

        window_spin = QSpinBox(dialog)
        window_spin.setRange(0, 1000)
        window_spin.setSingleStep(2)
        window_spin.setValue(self.savgol_window_points)

        poly_spin = QSpinBox(dialog)
        poly_spin.setRange(1, 99)
        poly_spin.setValue(self.savgol_polyorder)

        layout.addRow('Tamanho da janela:', window_spin)
        layout.addRow('Grau do polinômio:', poly_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        layout.addRow(buttons)

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.Accepted:
            return

        window_points = int(window_spin.value())
        polyorder = int(poly_spin.value())

        if window_points % 2 == 0:
            window_points = max(0, window_points - 1)  # Garante que seja ímpar e positivo

        if polyorder >= window_points and window_points > 0:
            QMessageBox.warning(self, 'Filtro Savitzky-Golay', 'O grau do polinômio deve ser menor que o número de pontos da janela.')
            return

        self.savgol_window_points = window_points
        self.savgol_polyorder = polyorder
        logger.info(f"Filtro Savitzky-Golay configurado: janela={self.savgol_window_points}, polinômio={self.savgol_polyorder}")
        self._save_persistent_settings()

        if self.spectra_data:
            x, y = zip(*self.spectra_data)
            self._plot_spectrum_curve(np.asarray(x, dtype=float), np.asarray(y, dtype=float))

    def select_mean_samples(self):
        """
        Abre um diálogo para selecionar o número de espectros a serem utilizados na média espectral.

        """
        value, accepted = QInputDialog.getInt(
            self,
            'Média espectral',
            'Número de espectros:',
            self.mean_samples,
            1,
            10000 if self.config_data.get('inter') != 'THORLABS OSA203' else 1,
            1,
        )
        if not accepted:
            return

        self.mean_samples = int(value)
        logger.info(f"Número de amostras para mean configurado: {self.mean_samples}")
        self._save_persistent_settings()

    def select_peak_parameters(self):
        """
        Abre um diálogo para ajustar os parâmetros de detecção.
        
        Parâmetros:
        - Prominência: altura mínima do pico em dBm
        - Largura: largura mínima do pico em pontos (samples)
        - Distância: distância mínima entre picos em pontos (samples)

        """
        is_int = self._fiber_mode() == 'INT'
        feature_label = 'vales' if is_int else 'picos'

        dialog = QDialog(self)
        dialog.setWindowTitle(f'Detecção de {feature_label}')
        layout = QFormLayout(dialog)

        prominence_spin = QDoubleSpinBox(dialog)
        prominence_spin.setRange(0.0, 100_000)
        prominence_spin.setDecimals(2)
        prominence_spin.setLocale(QLocale(QLocale.English)) # Separador decimal como ponto
        prominence_spin.setValue(float(self.peak_detection_params['prominence']))

        width_spin = QSpinBox(dialog)
        width_spin.setRange(0, 100_000)
        width_spin.setLocale(QLocale(QLocale.English)) # Separador decimal como ponto
        width_spin.setValue(int(self.peak_detection_params['width']))

        distance_spin = QSpinBox(dialog)
        distance_spin.setRange(1, 100_000)
        distance_spin.setValue(int(self.peak_detection_params['distance']))

        layout.addRow('Prominência (dBm):', prominence_spin)
        layout.addRow('Largura (pontos):', width_spin)
        layout.addRow('Distância (pontos):', distance_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        layout.addRow(buttons)

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.Accepted:
            return

        self.peak_detection_params = {
            'prominence': float(prominence_spin.value()),
            'width': float(width_spin.value()),
            'distance': int(distance_spin.value()),
        }
        self._save_persistent_settings()
        logger.info(
            "Parâmetros de %s configurados: prominence=%s dBm, width=%s pt, distance=%s pt",
            'vale' if is_int else 'pico',
            self.peak_detection_params['prominence'],
            self.peak_detection_params['width'],
            self.peak_detection_params['distance'],
        )

    def _visible_spectrum_brush(self, x_values: np.ndarray):
        """
        Cria um pincel com gradiente para representar a faixa visível do espectro.
        Args:
            x_values (np.ndarray): Array de valores do eixo X (comprimento de onda) para o espectro atual.
        Returns:
            QBrush: Pincel com gradiente colorido para a faixa visível, ou None se os valores de x forem inválidos.

        """
        if x_values.size == 0:
            return None

        x_min = float(np.min(x_values))
        x_max = float(np.max(x_values))
        if x_max <= x_min:
            return None

        visible_min = float(self._from_meter(np.array([380e-9]), self.xUnit)[0])
        visible_max = float(self._from_meter(np.array([730e-9]), self.xUnit)[0])

        span = x_max - x_min

        spectrum_stops_nm = [
            (380, '#6a00ff'),
            (440, '#0047ff'),
            (490, '#00c8ff'),
            (510, '#00ff7f'),
            (580, '#ffff00'),
            (640, '#ff7f00'),
            (730, '#ff0000'),]

        gradient = QLinearGradient(x_min, 0.0, x_max, 0.0)
        gradient.setCoordinateMode(QGradient.LogicalMode)

        # Fora do visível = transparente
        transparent = QColor(0, 0, 0, 0)

        start_rel = (visible_min - x_min) / span
        end_rel = (visible_max - x_min) / span

        eps = 1e-9
        clamp_rel = lambda p: float(np.clip(p, 0.0, 1.0))

        gradient.setColorAt(0.0, transparent)
        gradient.setColorAt(clamp_rel(start_rel - eps), transparent)

        # Espectro visível
        for wl_nm, color_hex in spectrum_stops_nm:
            wl = float(self._from_meter(np.array([wl_nm * 1e-9]), self.xUnit)[0])
            rel = (wl - x_min) / span

            if 0.0 <= rel <= 1.0:
                gradient.setColorAt(clamp_rel(rel), QColor(color_hex))

        gradient.setColorAt(clamp_rel(end_rel + eps), transparent)
        gradient.setColorAt(1.0, transparent)

        return QBrush(gradient)

    def _plot_spectrum_curve(self, x_values: np.ndarray, y_values: np.ndarray):
        """
        Plota o(s) espectro(s) e um preenchimento colorido para a faixa visível.
        Args:
            x_values (np.ndarray): Array de valores do eixo X (comprimento de onda) para o espectro atual.
            y_values (np.ndarray): Array de valores do eixo Y (intensidade) para o espectro atual.
        
        """
        self.spectraPlotWidget.clear()
        self.spectraPlotWidget.addItem(self.roi_region)

        y_values = preprocess_plot_data(y_values, self.window_methods, 
                                        self.savgol_window_points, self.savgol_polyorder, self.window)

        brush = self._visible_spectrum_brush(x_values)
        plot_kwargs = {'pen': pg.mkPen(self.theme_colors['spectrum'], width=1),}
        if brush is not None:
            plot_kwargs['fillLevel'] = float(np.min(y_values))
            plot_kwargs['brush'] = brush

        try:
            self.spectraPlotWidget.plot(x_values, y_values, **plot_kwargs)
        except Exception as e:
            self._show_error(f"Erro ao plotar espectro: {str(e)}")

        for button in self._list_fix_buttons():
            if button.isChecked():
                if str(button) not in self.fixed_traces:
                    continue
                color = button.palette().color(QPalette.ColorRole.Button)
                x = np.asarray(self.fixed_traces[str(button)][0], dtype=float)
                y = preprocess_plot_data(np.asarray(self.fixed_traces[str(button)][1], dtype=float), self.window_methods, 
                                         self.savgol_window_points, self.savgol_polyorder, self.window)
                self.spectraPlotWidget.plot(x, y, pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine))

    def _list_fix_buttons(self) -> list[QPushButton]:
        """
        Returns:
            list: lista com os botões de fixação presentes na interface.

        """
        buttons = []
        for i in range(self.fixedTraces_vlay.count()):
            item = self.fixedTraces_vlay.itemAt(i)
            widget = item.widget()
            buttons.append(widget)
        return buttons

    def set_theme(self, theme: str, persist: bool = True):
        """
        Aplica tema claro/escuro para widgets Qt e gráficos pyqtgraph.
        Args:
            theme (str): Nome do tema a ser aplicado ('light' ou 'dark').

        """
        if theme not in ('light', 'dark'):
            return
        if self.config_data is None:
            self.config_data = {}
        self.theme = theme
        if theme == self.config_data.get('theme') and self.theme_colors:
            self.actionLight.setChecked(theme == 'light')
            self.actionDark.setChecked(theme == 'dark')
            if persist:
                self._save_persistent_settings()
            return

        self.config_data['theme'] = theme
        if theme == 'dark':
            self.theme_colors = {
                'plot_bg': '#0b1220',
                'axis': '#e5e7eb',
                'roi_spec': '#64a5fa3c',
                'roi_temp': '#4bdca037',
                'spectrum': '#19867d',
                'accent': "#961313",}
        else:
            self.theme_colors = {
                'plot_bg': '#ffffffff',
                'axis': '#1f2937',
                'roi_spec': '#2864eb32',
                'roi_temp': '#19a04b2d',
                'spectrum': '#17bdaf',
                'accent': '#961313',}

        self.actionLight.setChecked(theme == 'light')
        self.actionDark.setChecked(theme == 'dark')

        QApplication.instance().setStyleSheet(qdarktheme.load_stylesheet(theme))

        btn_colors =         [QColor(139, 0, 0),
                              QColor(0, 152, 0),
                              QColor(0, 0, 188),
                              QColor(255, 170, 0),
                              QColor(4, 150, 143),
                              QColor(144, 1, 124),]
        btn_colors_checked = [QColor(139, 50, 50),
                              QColor(90, 152, 90),
                              QColor(70, 70, 188),
                              QColor(200, 169, 106),
                              QColor(109, 150, 148),
                              QColor(119, 66, 124),]
        
        for i, button in enumerate((self._list_fix_buttons())):
            color = btn_colors[i]
            color_chk = btn_colors_checked[i]
            button.setStyleSheet(
                f"""QPushButton {{background-color: rgb({color_chk.red()}, {color_chk.green()}, {color_chk.blue()}); color: lightgray;}}
                QPushButton:checked {{background-color: rgb({color.red()}, {color.green()}, {color.blue()}); color: white;}}""")

        for widget in (self.spectraPlotWidget, self.temporalPlotWidget, self.boxPlotWidget):
            widget.setBackground(self.theme_colors['plot_bg'])
        
        for plot_widget, _ in [*getattr(self, 'merge_plot_specs', []), *getattr(self, 'bragg_merge_plot_specs', [])]:
            if plot_widget is not None:
                plot_widget.setBackground(self.theme_colors['plot_bg'])

        for plot_widget in (
            self.spectraPlotWidget,
            self.temporalPlotWidget,
            *[spec[0] for spec in getattr(self, 'merge_plot_specs', [])],
            *[spec[0] for spec in getattr(self, 'bragg_merge_plot_specs', [])],
        ):
            if plot_widget is None:
                continue
            item = plot_widget.getPlotItem()
            item.getAxis('left').setPen(pg.mkPen(self.theme_colors['axis']))
            item.getAxis('left').setTextPen(pg.mkPen(self.theme_colors['axis']))
            item.getAxis('bottom').setPen(pg.mkPen(self.theme_colors['axis']))
            item.getAxis('bottom').setTextPen(pg.mkPen(self.theme_colors['axis']))

        if self.roi_region:
            self.roi_region.setBrush(self.theme_colors['roi_spec'])
            self.temporal_roi_region.setBrush(self.theme_colors['roi_temp'])

        if self.results_df['Timestamp']:
            self._update_plots_with_results()

        self._setup_merge_plots()
        self._refresh_merge_views()
        if persist:
            self._save_persistent_settings()

    def open_file(self):
        """
        Abre um diálogo para seleção do arquivo de dados e carrega os dados selecionados.
        
        """
        file_path = prompt_open_file(self)
        if not file_path:
            return

        self.load_file(file_path)

    def open_new_window(self):
        """
        Abre nova janela limpa e fecha o arquivo atual, se houver.

        """
        merge_index = self.tabWidget.indexOf(getattr(self, 'tab_merge'))
        self.channel_states = {
            i: self._default_channel_state()
            for i in range(self.tabWidget.count())
            if i != merge_index
        }
        current_index = self.tabWidget.currentIndex()
        if current_index == merge_index:
            enabled_tabs = self._enabled_tab_indices()
            current_index = enabled_tabs[0] if enabled_tabs else 0
            self.tabWidget.setCurrentIndex(current_index)
        self._restore_channel_state(current_index)
        self.clear_plot()
        self.boxPlot.clear()
        self.boxLegend.clear()
        self._refresh_merge_views()
        self.setWindowTitle("Análise de dados")

    def load_config(self, config: dict):
        """
        Recebe os dados de configuração da ConfigWindow, habilita as abas e inicia a leitura dos espectros.
        
        Args:
            config_data (Dict): Dicionário contendo os parâmetros de configuração.
            
        """
        self.config_data = config
        self.config_data['theme'] = self.theme

        if self._is_braggmeter():
            # Conecta a mudança de subaba de trace ao handler
            self.tab_bragg.currentChanged.connect(self._on_bragg_trace_changed)
            selected_traces = self._active_bragg_traces()
            for trace_idx in range(self.tab_bragg.count()):
                is_merge_tab = trace_idx == self.tab_bragg.indexOf(getattr(self, 'bragg_merge', None))
                self.tab_bragg.setTabEnabled(trace_idx, is_merge_tab or trace_idx in selected_traces)
        else:
             # Remove da tab interna
            idx = self.tab_bragg.indexOf(self.main_qwt)
            if idx != -1:
                self.tab_bragg.removeTab(idx)

            # Move para a tab externa
            self.main_qwt.setParent(None)
            self.tab_ch1.layout().addWidget(self.main_qwt)
            
            # Remove o tab widget interno
            self.tab_bragg.hide()
 
        # Aplica o tema
        self.set_theme(self.theme, persist=False)

        # Aplica a unidade persistida do eixo X
        self.actionM.setChecked(self.xUnit == 'm')
        self.actionUm.setChecked(self.xUnit == 'um')
        self.actionNm.setChecked(self.xUnit == 'nm')
        self.actionPm.setChecked(self.xUnit == 'pm')
        self._set_spectrum_axis_unit()

        # Ajusta a unidade do eixo y
        inter = config.get('inter')
        if inter in ('IBSEN IMON-512', 'BRAGGMETER FS22DI HBM'):
            self.spectraPlotWidget.setLabel('left', 'Potência', units='u.a.')

        channels = config.get('channels')
        merge_index = self.tabWidget.indexOf(getattr(self, 'tab_merge'))
        channel_tabs = [i for i in range(self.tabWidget.count()) if i != merge_index]
        for channel_pos, tab in enumerate(channel_tabs):
            enabled = bool(channels[channel_pos]) if channel_pos < len(channels) else False
            self.tabWidget.setTabEnabled(tab, enabled) # Habilita as abas selecionas na configuração

        self.channel_states = {
            i: self._default_channel_state()
            for i in channel_tabs
        }
        self.enabled_channels = self._enabled_tab_indices()

        self._load_interface_specific_settings()

        first_enabled = self.enabled_channels[0] if self.enabled_channels else 0
        self.tabWidget.setCurrentIndex(first_enabled)
        if self._is_braggmeter():
            trace_numbers = self._active_bragg_traces()
            initial_trace = trace_numbers[0] if trace_numbers else 0
            self._restore_channel_state(first_enabled, trace=initial_trace)
        else:
            self._restore_channel_state(first_enabled)
        self._apply_fiber_mode()
        self._load_fiber_specific_settings()
        if self._fiber_mode() != 'FBG':
            self._restore_roi_for_current_mode()
        self._refresh_active_channel_view()
        self._refresh_merge_views()
        self._save_persistent_settings()

        self._run()

    def unit_changed(self, unit: str):
        """
        Callback chamado quando a unidade do eixo X é alterada.
        Atualiza os dados e os gráficos para refletir a nova unidade.
        Args:
            unit (str): Nova unidade selecionada ('m', 'um', 'nm', 'pm').
        
        """
        old_unit = self.xUnit
        if unit == old_unit:
            self.actionM.setChecked(unit == 'm')
            self.actionUm.setChecked(unit == 'um')
            self.actionNm.setChecked(unit == 'nm')
            self.actionPm.setChecked(unit == 'pm')
            return

        old_to_m = self._unit_to_meter_factor(old_unit)
        new_to_m = self._unit_to_meter_factor(unit)
        scale_old_to_new = old_to_m / new_to_m

        self.xUnit = unit
        self.actionM.setChecked(unit == 'm')
        self.actionUm.setChecked(unit == 'um')
        self.actionNm.setChecked(unit == 'nm')
        self.actionPm.setChecked(unit == 'pm')
        self._set_spectrum_axis_unit()

        for tab_idx, state in self.channel_states.items():
            roi_range = state.get('roi_range')
            if roi_range is not None:
                state['roi_range'] = [roi_range[0] * scale_old_to_new, roi_range[1] * scale_old_to_new]

            spectra = state.get('spectra_data')
            if spectra:
                x_old, y_vals = zip(*spectra)
                x_new = np.asarray(x_old, dtype=float) * scale_old_to_new
                y_new = np.asarray(y_vals, dtype=float)
                state['spectra_data'] = list(zip(x_new, y_new))

            fixed_traces = state.get('fixed_traces', {})
            for btn, trace_item in list(fixed_traces.items()):
                x_old, y_vals = trace_item
                x_new = np.asarray(x_old, dtype=float) * scale_old_to_new
                fixed_traces[btn] = (x_new, np.asarray(y_vals, dtype=float))

            results_df = state.get('results_df', {})
            if 'Vale' in results_df and results_df.get('Vale'):
                results_df['Vale'] = [float(value) * scale_old_to_new for value in results_df['Vale']]
            if 'Picos' in results_df and results_df.get('Picos'):
                results_df['Picos'] = [
                    [float(value) * scale_old_to_new for value in peak_values]
                    for peak_values in results_df['Picos']
                ]

            pending_hdf5 = state.get('pending_hdf5', {})
            if 'Vale' in pending_hdf5 and pending_hdf5.get('Vale'):
                pending_hdf5['Vale'] = [float(value) * scale_old_to_new for value in pending_hdf5['Vale']]
            if 'Picos' in pending_hdf5 and pending_hdf5.get('Picos'):
                pending_hdf5['Picos'] = [
                    [float(value) * scale_old_to_new for value in peak_values]
                    for peak_values in pending_hdf5['Picos']
                ]

            if tab_idx == self.active_channel_idx:
                self._restore_channel_state(tab_idx)

        self._refresh_active_channel_view()
        self._refresh_merge_views()
        self._save_persistent_settings()

    def _clear_layout(self, layout):
        """
        Remove recursivamente todos os widgets e sublayouts de um layout, e depois o próprio layout.
        Args:
            layout (QLayout): O layout a ser limpo.
        
        """
        if not hasattr(self, layout.__class__.__name__):
            return
        while layout.count():
            item = layout.takeAt(0)

            widget = item.widget()
            child_layout = item.layout()

            if widget is not None:
                widget.deleteLater()

            elif child_layout is not None:
                self._clear_layout(child_layout)

        layout.deleteLater()

    def _run(self):
        """
        Inicia a thread de aquisição de dados.

        """
        if self.thread is not None:
            return

        self.sample_rate = max(0, int(self.sr_spin.value()))
        self._save_persistent_settings()
            
        port = self.config_data.get('port')
        inter = self.config_data.get('inter')
        ip = self.config_data.get('ip')
        range = self.config_data.get('range')
        res = self.config_data.get('res')
        self.fixed_wavelengths = np.arange(range[0], range[1], res) # Atualiza os comprimentos de onda fixos para interpolação

        if self.continuous_chk.isChecked():
            if not self.continuous_cfg():
                return

        # Obtém a instância compartilhada, se necessário
        if self.osa is None and 'THORLABS' in inter:
            self.osa = self.config_data.get('sdk')

        # Inicia a thread de aquisição de dados
        self.thread = QThread()
        switch_ports = self.config_data.get('switch_ports', [])
        self.worker = DataAcquisition(inter, ip, port, osa=self.osa, switch_ports=switch_ports)
        self.worker.moveToThread(self.thread)

        # Conecta os sinais e slots da thread e do worker
        self.thread.started.connect(self.worker.run)
        self.request_data_signal.connect(self.worker.request_data, Qt.QueuedConnection)
        self.stop_worker_signal.connect(self.worker.stop, Qt.QueuedConnection)
        self.worker.data_acquired.connect(self._handle_data_acquired)
        self.worker.finished.connect(self._cleanup_thread)
        self.worker.error_occurred.connect(self._show_error)
        self.worker.start_thread.connect(self._thread_started)
        time.sleep(.1)
        
        self.thread.start()

        self._running = True
        self.stop_btn.setText("Parar")
        self.stop_btn.setStyleSheet("QPushButton { background-color: #fd4d4d; color: white; }")
        self._is_stopping = False
        self.cfg_spin.setEnabled(False) # Desabilita o controle de tempo de exposição
        self.cfg_lbl.setEnabled(False)
        self.sr_spin.setEnabled(False) # Desabilita o controle de intervalo entre amostras
        self.sr_lbl.setEnabled(False)
        self.continuous_chk.setEnabled(False)
        self.continuous_lbl.setEnabled(False)

    def _thread_started(self):
        """
        Callback chamado quando a thread de aquisição de dados é iniciada.
        
        """
        logger.info("Thread de aquisição de dados iniciada.")

        if self.continuous_chk.isChecked() and self.flush_timer is None:
            self.flush_timer = QTimer(self)
            self.flush_timer.timeout.connect(self._flush_continuous_buffer)
            self.flush_timer.start(self.flush_interval_ms)

        # Configura o timer para solicitar dados como disparo único
        self._arm_request_timer(self.sample_rate)

        match self.config_data.get('inter'):
            case 'IBSEN IMON-512':
                self.cfg_lbl.setText("Tempo de Exposição (µs)")
                self.cfg_spin.setRange(3, 65535) # Limita o tempo de exposição
                self.cfg_spin.setSingleStep(100)
            case 'BRAGGMETER FS22DI': # Remove o controle de tempo de exposição
                self._clear_layout(self.et_vlay)
                self.exposure_time = -1
            case 'BRAGGMETER FS22DI HBM': # Remove o controle de tempo de exposição
                self._clear_layout(self.et_vlay)
                self.exposure_time = -1
            case 'THORLABS CCT11':
                self.cfg_lbl.setText("Tempo de Exposição (µs)")
                self.cfg_spin.setRange(1000, 30000000) # Limita o tempo de exposição
                self.cfg_spin.setSingleStep(100)
            case 'THORLABS OSA203':
                self.cfg_lbl.setText("Tempo de Exposição (µs)")
                self.exposure_time = -1

        if self.exposure_time >= 0:
            if self.exposure_time == 0:
                self.cfg_spin.setValue(self.worker.get_exposure_time()) # Obtém o tempo de exposição atual
                self.exposure_time = self.cfg_spin.value()
            elif self.exposure_time != self.cfg_spin.value():
                self.worker.set_exposure_time(self.cfg_spin.value()) # Altera o tempo de exposição
                self.exposure_time = self.cfg_spin.value()

    def _pause_thread(self):
        """
        Pausa a aquisição sem fechar o hardware para permitir retomada imediata.

        """
        if self.timer is not None and self.timer.isActive():
            self.timer.stop()

        if self.worker is not None:
            try:
                self.worker.pause()
            except Exception:
                pass

        self._running = False
        self.stop_btn.setText("Retomar")
        self.stop_btn.setStyleSheet("QPushButton { background-color: #60fa93; color: #0f172a; }")
        self.sr_spin.setEnabled(True)
        self.sr_lbl.setEnabled(True)
        if self.config_data.get('inter') != 'THORLABS OSA203' and self.exposure_time >= 0:
            self.cfg_spin.setEnabled(True)
            self.cfg_lbl.setEnabled(True)
        self.continuous_chk.setEnabled(True)
        self.continuous_lbl.setEnabled(True)
        QApplication.instance().restoreOverrideCursor()
        self.stop_btn.setEnabled(True)

    def _resume_thread(self):
        """
        Retoma a aquisição usando a mesma thread/worker já inicializados.

        """
        if self._connection_lost:
            if self.thread is not None or self.worker is not None:
                self._cleanup_thread()
            self._run()
            return

        if self.thread is None or self.worker is None:
            self._run()
            return

        # Reaplica o modo contínuo sem recriar thread/worker para evitar
        # fechamento e reabertura da porta serial no Retomar.
        if self.continuous_chk.isChecked():
            if not self.continuous_cfg():
                QApplication.instance().restoreOverrideCursor()
                self.stop_btn.setEnabled(True)
                return

        try:
            self.worker.resume()
        except Exception:
            pass

        self.sample_rate = self.sr_spin.value()

        self._arm_request_timer(self.sample_rate)

        self._running = True
        self.stop_btn.setText("Parar")
        self.stop_btn.setStyleSheet("QPushButton { background-color: #fd4d4d; color: white; }")
        self.cfg_spin.setEnabled(False)
        self.cfg_lbl.setEnabled(False)
        self.sr_spin.setEnabled(False)
        self.sr_lbl.setEnabled(False)
        self.continuous_chk.setEnabled(False)
        self.continuous_lbl.setEnabled(False)

    def _show_error(self, title: str, message: str):
        """
        Mostra uma caixa de diálogo de erro de forma não-bloqueante.
        Args:
            title (str): Título da caixa de diálogo.
            message (str): Mensagem de erro a ser exibida.
        
        """
        QApplication.instance().restoreOverrideCursor()
        if 'max retries' in message.lower() or 'timeout' in message.lower():
            self._connection_lost = True

        QMessageBox.warning(self, title, message)
        # Em caso de erro de comunicação, apenas interrompe a aquisição
        # mantendo a janela aberta para análise offline.
        try:
            self._cleanup_thread()
        except Exception:
            pass

    def _show_warning(self, message, channel: int | None = None, trace_number: int | None = None):
        """
        Mostra uma caixa de diálogo de aviso de forma não-bloqueante.
        Args:
            message (str): Mensagem de aviso a ser exibida.
            channel (int | None): Número do canal (1-4).
        
        """
        # Determina o índice do canal para este aviso (usa o canal ativo se não fornecido)
        target_idx = self.active_channel_idx if channel is None else self._channel_index_from_number(channel)
        if trace_number is None:
            trace_number = self._active_trace_number()

        if message:
            # Garante que o botão/ícone exista (indicador minimizado visível para todos os canais)
            if not self.warning_btn:
                self.warning_btn = QPushButton()
                self.warning_hlay.addWidget(self.warning_btn)
                cur_dir = os.path.dirname(os.path.abspath(argv[0]))
                icon = os.path.join(cur_dir, "img", "warning.png")
                self.warning_btn.setIcon(QIcon(icon))
                # Ao clicar, mostra os avisos agregados do canal ativo
                self.warning_btn.clicked.connect(lambda: QMessageBox.warning(self, "Aviso", '\n'.join(self._gather_active_warnings())))

            if isinstance(message, (list, tuple, set)):
                raw_messages = message
            else:
                raw_messages = str(message).split(',')

            messages = [str(m).strip() for m in raw_messages if str(m).strip()]

            # Adiciona sufixo do trace se for BraggMeter
            if self._is_braggmeter():
                if trace_number is not None:
                    messages = [f"{msg} (Trace {trace_number})" for msg in messages]
                else:
                    traces = self._active_bragg_traces()
                    messages = [f"{msg} (Trace {traces[i]})" for i, msg in enumerate(messages)]

            # Garante que o estado do canal tenha a lista de avisos
            state = self.channel_states.setdefault(target_idx, self._default_channel_state())
            if self._is_braggmeter() and trace_number is not None and isinstance(state.get('trace_states'), dict):
                trace_state = state['trace_states'].setdefault(trace_number, self._default_trace_state(trace_number))
                channel_warnings = trace_state.setdefault('warnings', [])
            else:
                channel_warnings = state.setdefault('warnings', [])

            for msg in messages:
                # Adiciona à lista minimizada deste canal para que o ícone indique
                if msg not in channel_warnings:
                    channel_warnings.append(msg)

                # Exibe o popup apenas se nenhum outro canal já tiver mostrado a mesma mensagem
                if msg not in self._warning_popup_shown:
                    # Registra qual canal exibiu o popup
                    self._warning_popup_shown[msg] = target_idx
                    QMessageBox.warning(self, "Aviso", msg)

        else:
            # Se não houver mensagem, remove o botão e limpa avisos de todos os canais
            if self.warning_btn is not None:
                self.warning_hlay.removeWidget(self.warning_btn)
                try:
                    self.warning_btn.clicked.disconnect()
                except Exception:
                    pass
                self.warning_btn.deleteLater()
                self.warning_btn = None
            # Limpa avisos por canal e o registro de popups exibidos
            for st in self.channel_states.values():
                if 'warnings' in st:
                    st['warnings'].clear()
                if isinstance(st.get('trace_states'), dict):
                    for trace_state in st['trace_states'].values():
                        if isinstance(trace_state, dict) and 'warnings' in trace_state:
                            trace_state['warnings'].clear()
            self._warning_popup_shown.clear()

    def _gather_active_warnings(self) -> list[str]:
        """Retorna os avisos agregados para o canal atualmente ativo."""
        state = self.channel_states.get(self.active_channel_idx, {})
        if self._is_braggmeter() and isinstance(state.get('trace_states'), dict):
            trace_number = self._active_trace_number()
            if trace_number is not None:
                trace_state = state['trace_states'].get(trace_number)
                if trace_state is not None:
                    return trace_state.get('warnings', [])
        return state.get('warnings', [])

    def _update_fixed_list(self, button: QPushButton):
        """
        Obtém o espectro fixado associado ao botão, remove-o da lista de espectros fixados e atualiza a interface.
        Args:
            button (QPushButton): Botão de fixação cujo espectro deve ser removido.

        """
        trace_number = self._active_trace_number() if self._is_braggmeter() else None
        if self._is_braggmeter() and trace_number is not None:
            channel_state = self.channel_states.setdefault(self.active_channel_idx, self._default_channel_state())
            trace_state = channel_state['trace_states'].setdefault(trace_number, self._default_trace_state(trace_number))
            trace_state.get('fixed_traces', {}).pop(str(button), None)
            if str(button) in trace_state.get('active_traces', []):
                trace_state['active_traces'] = [name for name in trace_state['active_traces'] if name != str(button)]
            self.fixed_traces = trace_state['fixed_traces']
            self.active_traces = trace_state['active_traces']
        else:
            self.fixed_traces.pop(str(button), None)

        idx = self._list_fix_buttons().index(button)
        item = self.fixedLines_vlay.itemAt(idx)
        line = item.widget()
        line.setStyleSheet("background-color: transparent")
        if button.isChecked():
            button.setChecked(False)
            if not self._running:
                x_vals, y_vals = zip(*self.spectra_data)
                # Redesenha o espectro atual para limpar as curvas fixadas
                self._plot_spectrum_curve(np.asarray(x_vals, dtype=float), np.asarray(y_vals, dtype=float))
            self._save_active_channel_state(trace_number=trace_number)
        self._refresh_merge_views()

    def toggle_fix(self, button: QPushButton, pos):
        """
        Callback para exibir um menu de contexto ao clicar com o botão direito em um botão de fixação, 
        permitindo remover o espectro fixado.

        """
        menu = QMenu()

        if str(button) in self.fixed_traces:
            menu.addAction("Remover")
            menu.triggered.connect(lambda: self._update_fixed_list(button))
            menu.exec_(button.mapToGlobal(pos))

    def fix_btn_clicked(self, button: QPushButton, checked: bool):
        """
        Gerencia a lógica de fixar/desfixar um espectro ao clicar nos botões de fixação.
        Args:
            button (QPushButton): Botão de fixação que foi clicado.
            checked (bool): Estado do botão após o clique (True se marcado, False se desmarcado).

        """
        trace_number = self._active_trace_number() if self._is_braggmeter() else None
        channel_state = self.channel_states.setdefault(self.active_channel_idx, self._default_channel_state())
        trace_state = None
        if self._is_braggmeter() and trace_number is not None:
            trace_state = channel_state['trace_states'].setdefault(trace_number, self._default_trace_state(trace_number))

        if str(button) in self.fixed_traces:
            if not self._running:
                if checked:
                    x, y = self.fixed_traces[str(button)]
                    color = button.palette().color(QPalette.ColorRole.Button)
                    self.spectraPlotWidget.plot(x, 
                                                preprocess_plot_data(y, self.window_methods, 
                                                    self.savgol_window_points, self.savgol_polyorder, self.window),
                                                pen=pg.mkPen(color, width=1))
                else:
                    x_vals, y_vals = zip(*self.spectra_data)
                    # Redesenha o espectro atual para limpar as curvas fixadas
                    self._plot_spectrum_curve(np.asarray(x_vals, dtype=float), np.asarray(y_vals, dtype=float))
        elif checked:
            if self.spectraPlotWidget.getPlotItem().listDataItems() == []:
                button.setChecked(False) # Impede de marcar se não houver espectro para fixar
                return
            x, y = zip(*self.spectra_data)
            trace_payload = (np.asarray(x, dtype=float), np.asarray(y, dtype=float))
            if trace_state is not None:
                trace_state.setdefault('fixed_traces', {})[str(button)] = trace_payload
                trace_state.setdefault('active_traces', [])
                if str(button) not in trace_state['active_traces']:
                    trace_state['active_traces'].append(str(button))
                self.fixed_traces = trace_state['fixed_traces']
                self.active_traces = trace_state['active_traces']
            else:
                self.fixed_traces[str(button)] = trace_payload

            idx = self._list_fix_buttons().index(button)
            item = self.fixedLines_vlay.itemAt(idx)
            line = item.widget()
            color = button.palette().color(QPalette.ColorRole.Button)
            line.setStyleSheet(
                f"background-color: rgba({color.red()}, {color.green()}, {color.blue()}, 255)"
            )
            if not self._running:
                color = button.palette().color(QPalette.ColorRole.Button)
                self.spectraPlotWidget.plot(x, 
                                            preprocess_plot_data(y, self.window_methods, 
                                                self.savgol_window_points, self.savgol_polyorder, self.window),
                                            pen=pg.mkPen(color, width=1))
        else:
            idx = self._list_fix_buttons().index(button)
            item = self.fixedLines_vlay.itemAt(idx)
            line = item.widget()
            if str(button) in self.fixed_traces:
                color = button.palette().color(QPalette.ColorRole.Button)
                line.setStyleSheet(
                    f"background-color: rgba({color.red()}, {color.green()}, {color.blue()}, 90)"
                )
        if trace_state is not None:
            trace_state['fixed_traces'] = self.fixed_traces
            trace_state['active_traces'] = [str(btn) for btn in self._list_fix_buttons() if btn.isChecked()]
            self.active_traces = trace_state['active_traces']
        else:
            self.active_traces = [str(btn) for btn in self._list_fix_buttons() if btn.isChecked()]
        self._save_active_channel_state(trace_number=trace_number)
        self._refresh_merge_views()
            
    def _cleanup_thread(self):
        """
        Limpa e finaliza a thread de aquisição de dados.
        
        """
        if self._is_stopping:
            return
        self._is_stopping = True
        
        if self.timer is not None:
            if self.timer.isActive():
                self.timer.stop()
            try:
                self.timer.timeout.disconnect()
            except:
                pass
            self.timer.deleteLater()
            self.timer = None
        self._cycle_started_at = None
        self._cycle_pending_responses = 0

        if self.flush_timer is not None:
            if self.flush_timer.isActive():
                self.flush_timer.stop()
            try:
                self.flush_timer.timeout.disconnect()
            except:
                pass
            self.flush_timer.deleteLater()
            self.flush_timer = None

        self._flush_continuous_buffer(force=True)

        if self.continuous_timer is not None:
            if self.continuous_timer.isActive():
                self.continuous_timer.stop()
            try:
                self.continuous_timer.timeout.disconnect()
            except:
                pass
            self.continuous_timer.deleteLater()
            self.continuous_timer = None
            
        if self.worker is not None:
            try:
                self.worker.stop()
            except Exception:
                pass
            try:
                self.stop_worker_signal.emit()
            except Exception:
                pass
            try:
                self.request_data_signal.disconnect(self.worker.request_data)
            except:
                pass
            try:
                self.worker.data_acquired.disconnect()
                self.worker.finished.disconnect()
                # Evita que erros tardios durante o desligamento fechem/afetem a UI
                self.worker.error_occurred.disconnect()
            except:
                pass
            try:
                self.stop_worker_signal.disconnect(self.worker.stop)
            except:
                pass
            
        if self.thread is not None:
            self.thread.requestInterruption()
            self.thread.quit()
            if not self.thread.wait(3000):
                logger.warning("Terminating thread...")
                self.thread.terminate()
                self.thread.wait()
            self.thread = None
            
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None
            
        self._is_stopping = False
        self._running = False
        self.stop_btn.setText("Retomar")
        self.stop_btn.setStyleSheet("QPushButton { background-color: #60fa93; color: #0f172a; }")
        self.sr_spin.setEnabled(True) # Habilita o controle de intervalo entre amostras
        self.sr_lbl.setEnabled(True)
        if self.config_data.get('inter') != 'THORLABS OSA203' and self.exposure_time >= 0:
            self.cfg_spin.setEnabled(True) # Habilita o controle de tempo de exposição
            self.cfg_lbl.setEnabled(True)
        self.continuous_chk.setEnabled(True)
        self.continuous_lbl.setEnabled(True)
        QApplication.instance().restoreOverrideCursor()
        self.stop_btn.setEnabled(True)

    def roi_changed(self):
        """
        Callback chamado quando a região de interesse (ROI) é alterada.
        Atualiza o box plot mesmo com a aquisição parada.
        
        """
        temporal_roi = self.temporal_roi_region.getRegion()
        channel_state = self.channel_states.setdefault(self.active_channel_idx, self._default_channel_state())
        channel_state['temporal_roi_range'] = temporal_roi

        if self._is_braggmeter():
            trace_number = self._active_trace_number()
            if trace_number is not None:
                trace_state = channel_state['trace_states'].setdefault(trace_number, self._default_trace_state(trace_number))
                trace_state['temporal_roi_range'] = temporal_roi

        if self.timer is None or not self.timer.isActive():
            self._update_plots_with_results()

    def _spectrum_roi_changed(self):
        roi_min, roi_max = self.roi_region.getRegion()
        self.roi_range = [roi_min, roi_max]
        self._save_roi_for_current_mode()
        channel_state = self.channel_states.setdefault(self.active_channel_idx, self._default_channel_state())
        channel_state['roi_range'] = list(self.roi_range)

        if self._is_braggmeter():
            trace_number = self._active_trace_number()
            if trace_number is not None:
                trace_state = channel_state['trace_states'].setdefault(trace_number, self._default_trace_state(trace_number))
                trace_state['roi_range'] = list(self.roi_range)

    def toggle_thread(self):
        """
        Inicia ou para a thread de aquisição de dados com lógica de toggle.
        No modo contínuo, parar é definitivo (não há pausa, apenas interrupção).
        
        """
        if not self.continuous_chk.isChecked():
            QApplication.instance().setOverrideCursor(Qt.WaitCursor)
            self.stop_btn.setEnabled(False) # Evita múltiplos cliques durante a transição
        try:
            if self.continuous_chk.isChecked() and self._running:
                # No modo contínuo, parar é definitivo
                self._flush_continuous_buffer(force=True)
                self._cleanup_thread()
                QMessageBox.information(
                    self,
                    "Amostra Contínua",
                    f"A amostra \"{self.sample_name}\" foi interrompida e salva com sucesso."
                )
            elif self.thread is not None:
                if self._running:
                    self._pause_thread()
                else:
                    self._resume_thread()
            else:
                self._run()
        except Exception:
            QApplication.instance().restoreOverrideCursor()
            self.stop_btn.setEnabled(True)
            raise

    def _flush_continuous_buffer(self, force: bool = False):
        """
        Salva em lote o buffer do modo contínuo para reduzir overhead de I/O.
        Args:
            force (bool): Se True, força a gravação de todos os dados pendentes, ignorando o tamanho do lote.

        """
        targets = [self.active_channel_idx]
        if force and self.enabled_channels:
            targets = list(self.enabled_channels)

        for idx in targets:
            state = self.channel_states.get(idx)
            if state is None:
                continue

            trace_entries = []
            if self._is_braggmeter() and isinstance(state.get('trace_states'), dict):
                trace_entries = [
                    (trace_number, trace_state)
                    for trace_number, trace_state in sorted(state['trace_states'].items())
                    if trace_state is not None
                ]
            else:
                trace_entries = [(None, state)]

            for trace_number, trace_state in trace_entries:
                pending = trace_state['pending_hdf5']
                pending_count = len(pending['Timestamp'])
                if pending_count == 0:
                    continue
                if not force and pending_count < self.flush_batch_size:
                    continue

                try:
                    inter = self.config_data.get('inter') if self.config_data else None
                    file_path = self.file_path or (self.config_data.get('path') if self.config_data else None)
                    if not inter or not file_path:
                        continue

                    intensities = np.asarray(pending['Intensidade'], dtype=np.float32)
                    timestamps = np.asarray(pending['Timestamp'], dtype=np.float64)
                    result_key = self._result_key()
                    values = pending[result_key]
                    sample_name = self.sample_name or "Atual"
                    if trace_number is not None:
                        sample_name = f"{sample_name} - Trace {trace_number}"
                    trace_suffix = f" trace {trace_number}" if trace_number is not None else ""

                    append_samples(
                        range_cfg=self.config_data.get('range'),
                        res=self.config_data.get('res'),
                        file_path=file_path,
                        inter=inter,
                        intensities=intensities,
                        timestamps=timestamps,
                        values=values,
                        sample_name=sample_name,
                        dataset_name=result_key,
                    )

                    trace_state['pending_hdf5'] = self._empty_result_store()
                    if idx == self.active_channel_idx and trace_number == self._active_trace_number():
                        self.pending_hdf5 = trace_state['pending_hdf5']
                    logger.debug(f"Flush contínuo realizado no canal {idx + 1}{trace_suffix} com {pending_count} registro(s).")
                except Exception as e:
                    logger.error(f"Erro ao salvar buffer contínuo (canal {idx + 1}{trace_suffix}): {e}")

    def update_plot(self, data, warning, channel: int):
        """
        Atualiza o gráfico com os dados adquiridos.        
        Args:
            data: Lista de tuplas (para BraggMeter) ou espectro único
            warning: Lista de mensagens de aviso (para BraggMeter) ou string única
            channel (int): Número do canal de onde os dados foram adquiridos
        
        """
        if not self._running:
            return

        QApplication.instance().restoreOverrideCursor()
        self.stop_btn.setEnabled(True)
        def _is_bragg_trace_collection(payload) -> bool:
            if not self._is_braggmeter() or not isinstance(payload, (list, tuple)) or not payload:
                return False

            first_item = payload[0]
            if isinstance(first_item, (list, tuple)) and len(first_item) == 2:
                return not (
                    np.isscalar(first_item[0])
                    and np.isscalar(first_item[1])
                )

            if isinstance(first_item, np.ndarray):
                return first_item.ndim >= 2 or (first_item.ndim == 1 and first_item.size != 2)

            return False

        is_bragg_trace_collection = _is_bragg_trace_collection(data)

        if not (is_bragg_trace_collection and isinstance(warning, list)):
            self._show_warning(warning, channel)

        target_idx = self._channel_index_from_number(channel)
        if target_idx not in self.channel_states:
            self.channel_states[target_idx] = self._default_channel_state()

        current_visible_idx = self.active_channel_idx
        visible_trace = self._active_trace_number() if self._is_braggmeter() else None
        # Só salva e restaura quando o canal alvo é diferente do visível; caso contrário
        # os atributos de instância já contêm o estado correto e o clone desnecessário
        # de results_df (que cresce com o tempo) bloqueia o event loop sem necessidade.
        if target_idx != current_visible_idx:
            self._save_active_channel_state()
            self._restore_channel_state(target_idx, trace=visible_trace)

        if is_bragg_trace_collection:
            active_traces = self._active_bragg_traces()
            warning_items = warning if isinstance(warning, list) else [warning] * len(data)
            trace_payloads = []

            for trace_pos, trace_number in enumerate(active_traces):
                if trace_pos >= len(data):
                    break
                trace_warning = warning_items[trace_pos] if trace_pos < len(warning_items) else warning_items[-1]
                trace_payloads.append((trace_number, data[trace_pos], trace_warning))

            if visible_trace is not None:
                trace_payloads.sort(key=lambda item: item[0] == visible_trace)

            channel_state = self.channel_states[target_idx]

            for trace_number, trace_data, trace_warning in trace_payloads:
                trace_state = channel_state['trace_states'].setdefault(trace_number, self._default_trace_state(trace_number))
                self._apply_state_snapshot(trace_state, sync_regions=False)
                self._show_warning(trace_warning, channel, trace_number=trace_number)

                x, y = zip(*trace_data)
                x = np.asarray(x, dtype=float)
                y = np.asarray(y, dtype=float)
                x *= self._unit_to_meter_factor('nm') / self._unit_to_meter_factor(self.xUnit)
                self._set_spectrum_axis_unit()

                self.spectra_data = list(zip(x, y))
                if trace_number == visible_trace:
                    self._plot_spectrum_curve(x, y)

                if self.config_data.get('fiber') != 'FBG' and self.roi_range is None:
                    x_min = min(x)
                    x_max = max(x)
                    x_range = x_max - x_min
                    self.roi_range = [(x_min + 0.25 * x_range), (x_max - 0.25 * x_range)]
                    if trace_number == visible_trace:
                        self.roi_region.setRegion(self.roi_range)
                    else:
                        self.roi_region.blockSignals(True)
                        try:
                            self.roi_region.setRegion(self.roi_range)
                        finally:
                            self.roi_region.blockSignals(False)
                    logger.debug(f"ROI inicial definida para: {self.roi_range}")
                elif self.config_data.get('fiber') == 'FBG':
                    self.roi_range = None

                self.process_spectra(update_views=(trace_number == visible_trace))
                self._save_active_channel_state(save_button_state=False, trace_number=trace_number)

            no_switch_bragg = (
                self.config_data.get('inter') in ('BRAGGMETER FS22DI', 'BRAGGMETER FS22DI HBM')
                and not self.config_data.get('switch_ports')
            )
            if target_idx == current_visible_idx and visible_trace is not None:
                self._restore_channel_state(target_idx, trace=visible_trace)
                self._refresh_active_channel_view(sync_buttons=False)
            if target_idx != current_visible_idx and not no_switch_bragg:
                self._restore_channel_state(current_visible_idx)
                self._refresh_active_channel_view(sync_buttons=False)
            return

        x, y = zip(*data)
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        # Converte de nm para a unidade configurada, caso necessário
        x *= self._unit_to_meter_factor('nm') / self._unit_to_meter_factor(self.xUnit)
        self._set_spectrum_axis_unit()

        self.spectra_data = list(zip(x, y))
        self._plot_spectrum_curve(x, y)

        if not self.config_data.get('fiber') == 'FBG' and self.roi_range is None:
            x_min = min(x)
            x_max = max(x)
            x_range = x_max - x_min
            self.roi_range = [(x_min+0.25*x_range), (x_max-0.25*x_range)] # Intervalo fixo
            self.roi_region.setRegion(self.roi_range)
            logger.debug(f"ROI inicial definida para: {self.roi_range}")
        elif self.config_data.get('fiber') == 'FBG':
            self.roi_range = None

        self.process_spectra()

        self._save_active_channel_state(save_button_state=False)
        no_switch_bragg = (
            self.config_data.get('inter') in ('BRAGGMETER FS22DI', 'BRAGGMETER FS22DI HBM')
            and not self.config_data.get('switch_ports')
        )
        if target_idx != current_visible_idx and not no_switch_bragg:
            self._restore_channel_state(current_visible_idx)
            self._refresh_active_channel_view(sync_buttons=False)

    def process_spectra(self, update_views: bool = True):
        """
        Executa o algoritmo de detecção de picos para o espectro na ROI.
        
        """
        if not self.spectra_data:
            logger.warning("Não há espectros carregados para processar.")
            return

        # 1. Processa o espectro atual
        wavelength, power = zip(*self.spectra_data)
        wavelength = np.asarray(wavelength, dtype=float)
        power = np.asarray(power, dtype=float)

        # Aplica o mesmo pré-processamento configurado na visualização antes das análises.
        # Isso garante consistência entre o que é visto e o que é usado em find_peaks/fit.
        power_processed = preprocess_plot_data(
            power,
            self.window_methods,
            self.savgol_window_points,
            self.savgol_polyorder,
            self.window,
        )

        # Wavelength em metros para o processamento
        display_to_m = self._unit_to_meter_factor(self.xUnit)
        wavelength *= display_to_m

        interp_fn = interp1d(
            wavelength,
            power_processed,
            kind='linear',
            bounds_error=False,
            fill_value=(power_processed[0], power_processed[-1])
        )
        intensities = np.asarray(interp_fn(self.fixed_wavelengths), dtype=np.float32)
        now = datetime.now().timestamp()

        fiber_type = self._fiber_mode()
        if fiber_type in ('FBG', 'INT'):
            # No modo INT, restringe a detecção à ROI espectral selecionada.
            if fiber_type == 'INT':
                roi_min, roi_max = self.roi_region.getRegion()
                logger.debug(f"Processando espectros dentro da ROI: {roi_min:.2f} a {roi_max:.2f}")
                roi_m_min = roi_min * display_to_m
                roi_m_max = roi_max * display_to_m
                roi_mask = (wavelength >= roi_m_min) & (wavelength <= roi_m_max)
                wl_for_detection = wavelength[roi_mask]
                power_for_detection = power_processed[roi_mask]
            else:
                wl_for_detection = wavelength
                power_for_detection = power_processed

            if wl_for_detection.size == 0:
                logger.warning("ROI espectral vazia: nenhuma amostra disponível para detecção de picos/vales.")
                peak_results = []
            else:
                peak_results = find_wavelength_peaks(
                    wl_for_detection,
                    power_for_detection,
                    prominence=self.peak_detection_params['prominence'],
                    distance=self.peak_detection_params['distance'],
                    width=self.peak_detection_params['width'],
                    valley=(fiber_type == 'INT'),
                    fit_model='lorentz' if fiber_type == 'INT' else 'gaussian',
                ) or []

            peak_values = [float(item['wavelength']) for item in peak_results]

            self.results_df['Timestamp'].append(now)
            self.results_df['Intensidade'].append(intensities)
            self.results_df['Picos'].append(peak_values)

            # Trimming aplicado sempre (não apenas no modo contínuo) para impedir crescimento
            # ilimitado de results_df em sessões longas, independentemente do modo de operação.
            if len(self.results_df['Timestamp']) > self.max_live_points:
                excess = len(self.results_df['Timestamp']) - self.max_live_points
                self.results_df['Timestamp'] = self.results_df['Timestamp'][excess:]
                self.results_df['Intensidade'] = self.results_df['Intensidade'][excess:]
                self.results_df['Picos'] = self.results_df['Picos'][excess:]

            if self.continuous_chk.isChecked():
                self.pending_hdf5['Timestamp'].append(now)
                self.pending_hdf5['Intensidade'].append(intensities)
                self.pending_hdf5['Picos'].append(peak_values)
                self._flush_continuous_buffer()

            if update_views and len(self.results_df['Timestamp']) <= 1:
                self.spectraPlotWidget.autoRange() # Ajusta o zoom na primeira aquisição
            logger.debug(f"Processamento {fiber_type} concluído. Total de {len(self.results_df['Timestamp'])} medições acumuladas.")
        else: # LPG
            # 2. Obtém os limites da ROI selecionada pelo usuário
            roi_min, roi_max = self.roi_region.getRegion()
            logger.debug(f"Processando espectros dentro da ROI: {roi_min:.2f} a {roi_max:.2f}")

            roi_m_min = roi_min * display_to_m
            roi_m_max = roi_max * display_to_m

            # 3. Chama a função do backend para encontrar o pico
            res_wl = find_resonant_wavelength(np.array(wavelength), np.array(power_processed), roi_m_min, roi_m_max)
            
            # 4. Adiciona o resultado ao dicionário se um pico for encontrado
            if res_wl is not None:
                res_wl = float(res_wl)
                if len(self.results_df['Timestamp']) <= 1:
                     self.spectraPlotWidget.autoRange() # Ajusta o zoom na primeira aquisição

                self.results_df['Timestamp'].append(now)
                self.results_df['Intensidade'].append(intensities)
                self.results_df['Vale'].append(res_wl)

                # Trimming aplicado sempre (não apenas no modo contínuo) para impedir crescimento
                # ilimitado de results_df em sessões longas, independentemente do modo de operação.
                if len(self.results_df['Timestamp']) > self.max_live_points:
                    excess = len(self.results_df['Timestamp']) - self.max_live_points
                    self.results_df['Timestamp'] = self.results_df['Timestamp'][excess:]
                    self.results_df['Intensidade'] = self.results_df['Intensidade'][excess:]
                    self.results_df['Vale'] = self.results_df['Vale'][excess:]

                if self.continuous_chk.isChecked():
                    self.pending_hdf5['Timestamp'].append(now)
                    self.pending_hdf5['Intensidade'].append(intensities)
                    self.pending_hdf5['Vale'].append(res_wl)
                    self._flush_continuous_buffer()

                    logger.debug(f"Dado salvo automaticamente")

            logger.debug(f"Processamento concluído. Total de {len(self.results_df['Timestamp'])} medições acumuladas.")

        # 5. Chama a função para atualizar os gráficos com os resultados
        if update_views:
            self._update_plots_with_results()
    
    def _update_plots_with_results(self):
        """
        Atualiza os gráficos para exibir os resultados processados.
        - Plota os pontos no gráfico de evolução temporal.
        - Desenha linhas verticais nos picos encontrados no gráfico de espectros.
        
        """
        if not self.results_df['Timestamp']:
            if not self.samples:
                logger.warning("Nenhum resultado para exibir nos gráficos.")
            else:
                self.update_box_plot()
            return

        timestamps_numeric = self.results_df['Timestamp']

        # --- Atualiza Gráfico 2: Evolução Temporal ---
        self.temporalPlotWidget.clear()
        self.temporalPlotWidget.addItem(self.temporal_roi_region)

        if self._fiber_mode() in ('FBG', 'INT'):
            peak_series = self.results_df['Picos']
            is_int = self._fiber_mode() == 'INT'

            # Apenas picos recorrentes devem persistir em temporal/box (ruído momentâneo é descartado)
            recurring_groups = self._recurring_fbg_peak_groups(timestamps_numeric, peak_series, min_count=2)

            # Plota cada pico recorrente rastreado com sua cor consistente
            for peak_wavelength, points in recurring_groups.items():
                x_points = [pt[0] for pt in points]
                y_values_m = [pt[1] for pt in points]

                if x_points:
                    color = self._get_fbg_peak_color(peak_wavelength)
                    y_points = self._from_meter(y_values_m, self.resultUnit)
                    self.temporalPlotWidget.plot(
                        x=x_points,
                        y=y_points,
                        pen=pg.mkPen(color, width=1),
                        symbol='o',
                        symbolBrush=color,
                        symbolSize=7,
                    )

            if self.timer is not None and timestamps_numeric:
                roi_min = timestamps_numeric[0]
                roi_max = timestamps_numeric[-1]
                self.temporal_roi_region.setRegion((roi_min, roi_max))

            logger.debug("Gráfico de evolução temporal atualizado.")

            # Atualiza linhas verticais no gráfico de espectros para os últimos picos.
            # Remove apenas as linhas rastreadas em _peak_marker_lines, sem precisar
            # iterar sobre todos os items do plot para encontrá-las por tipo.
            for line in self._peak_marker_lines:
                self.spectraPlotWidget.removeItem(line)
            self._peak_marker_lines.clear()

            if self.results_df['Picos']:
                last_peaks = self.results_df['Picos'][-1]
                if last_peaks:
                    # Ordena os picos por wavelength
                    sorted_peaks = sorted(last_peaks)
                    # Garante que chaves mapeadas não sejam reutilizadas para
                    # múltiplos picos do mesmo espectro.
                    used_keys: set = set()
                    for peak_value in sorted_peaks:
                        # Mantém destaque de pico apenas para grupos recorrentes.
                        match_key = self._find_matching_peak_key(recurring_groups, peak_value)
                        if match_key is not None:
                            color = self._get_fbg_peak_color(match_key, used_keys)
                        else:
                            color = self._get_fbg_peak_color(peak_value, used_keys)
                        line = pg.InfiniteLine(
                            pos=self._from_meter([peak_value], self.xUnit)[0],
                            angle=90,
                            movable=False,
                            pen={'color': color, 'style': pg.QtCore.Qt.DashLine}
                        )
                        self.spectraPlotWidget.addItem(line)
                        self._peak_marker_lines.append(line)
            logger.debug("Marcadores de %s adicionados ao gráfico de espectros.", 'vale' if is_int else 'pico')

            current_peak_samples = self._fbg_current_peak_samples(
                timestamps_numeric,
                peak_series,
                label_prefix="Atual",
                feature_label="Vale" if is_int else "Pico",
            )

            if current_peak_samples:
                self.update_box_plot(current_peak_samples=current_peak_samples, same_sample=True)
            elif self.samples:
                self.update_box_plot()
            return

        valleys_m = np.asarray(self.results_df['Vale'], dtype=float)
        resonant_temporal = self._from_meter(valleys_m, self.resultUnit)
        resonant_spectrum = self._from_meter(valleys_m, self.xUnit)

        # Plota os pontos e uma linha conectando-os
        self.temporalPlotWidget.plot(
            x=timestamps_numeric,
            y=resonant_temporal,
            pen={'color': self.theme_colors['spectrum'], 'width': 2},
            symbol='o',
            symbolBrush=self.theme_colors['accent'],
            symbolSize=8
        )

        if self.timer is not None:
            roi_min = timestamps_numeric[0] # Primeiro timestamp
            roi_max = timestamps_numeric[-1] # Último timestamp
            self.temporal_roi_region.setRegion((roi_min, roi_max)) # Ajusta a ROI temporal

        logger.debug("Gráfico de evolução temporal atualizado.")

        # --- Atualiza Gráfico 1: Adiciona Marcadores de Pico ---
        # Remove apenas as linhas rastreadas, sem varrer todos os items do plot.
        for line in self._peak_marker_lines:
            self.spectraPlotWidget.removeItem(line)
        self._peak_marker_lines.clear()

        # Adiciona uma linha vertical para o último pico encontrado
        line = pg.InfiniteLine(
            pos=resonant_spectrum[-1],
            angle=90,
            movable=False,
            pen={'color': self.theme_colors['accent'], 'style': pg.QtCore.Qt.DashLine}
        )
        self.spectraPlotWidget.addItem(line)
        self._peak_marker_lines.append(line)

        # Seleciona a ROI temporal atual para filtrar os dados computados pelo box plot
        roi_min_ts, roi_max_ts = self.temporal_roi_region.getRegion()
        mask = [(r >= roi_min_ts) and (r <= roi_max_ts) for r in timestamps_numeric]
        boxplot_valleys = [resonant_temporal[i] for i in range(len(resonant_temporal)) if mask[i]]

        if not boxplot_valleys and timestamps_numeric:
            logger.warning("ROI temporal vazia para box plot, nada a plotar.")
            return

        self.update_box_plot(boxplot_valleys)

    def update_box_plot(self, boxplot_valleys: list = None, current_peak_samples: list[tuple[str, tuple]] | None = None, same_sample: bool = False):
        """
        Atualiza o box plot com os dados processados.
        Args:
            boxplot_valleys (list): Lista de valores com as estatísticas para o box plot.
            current_peak_samples (list[tuple[str, tuple]] | None): Lista de boxplots atuais já agregados por pico.
            same_sample (bool): Mantido por retrocompatibilidade.
            
        """
        self.boxPlot.clear()
        self.boxLegend.clear()

        samples = self._expand_samples_for_boxplot()
        if current_peak_samples:
            samples.extend(current_peak_samples)
        elif boxplot_valleys is not None:
            samples.append(("Atual", self.box_plot_statistics(boxplot_valleys)))

        def _sample_group_key(label: str) -> str:
            if " - Pico " in label:
                return label.split(" - Pico ", 1)[0]
            if " - Vale " in label:
                return label.split(" - Vale ", 1)[0]
            return label

        intra_sample_gap = 0.75
        inter_sample_gap = 1.9
        box_width = 0.5

        y_values = []
        prev_group = None
        x_center = 1.0

        for i, (sample_name, stats) in enumerate(samples):
            q1, q2, q3, lower_whisker, upper_whisker, outliers = stats
            y_values.extend([q1, q2, q3, lower_whisker, upper_whisker]) # amostras anteriores

            current_group = _sample_group_key(sample_name)
            if i > 0:
                same_group = current_group == prev_group
                x_center += intra_sample_gap if same_group else inter_sample_gap
            prev_group = current_group

            # Cores alternadas para os box plots
            rgb = [(50*i if (i%3) == 1 else 0) % 250,
                   (50*i if (i%3) == 2 else 0) % 250,
                   (50*i if (i%3) == 0 else 0) % 250]

            # --- Desenho do box plot ---
            # Retângulo entre Q1 e Q3
            box = QGraphicsRectItem(x_center - box_width / 2, q1, box_width, q3-q1)
            box.setPen(pg.mkPen('k'))
            box.setBrush(pg.mkBrush(rgb)) # cores alternadas
            self.boxPlot.addItem(box)

            # Legenda
            legend = pg.PlotDataItem([0], [1], pen=None, symbol='s', symbolBrush=pg.mkBrush(rgb))
            self.boxLegend.addItem(legend, sample_name)

            # Linha da mediana
            self.boxPlot.plot([x_center - box_width / 2, x_center + box_width / 2], [q2, q2], pen=pg.mkPen(self.theme_colors['accent'], width=2))
            #Linha vertical
            self.boxPlot.plot([x_center, x_center], [q1, q3], pen='k')
            # Whiskers (linhas verticais dos limites)
            self.boxPlot.plot([x_center, x_center], [q3, upper_whisker], pen='k')
            self.boxPlot.plot([x_center, x_center], [q1, lower_whisker], pen='k')
            # Topo e base dos whiskers
            self.boxPlot.plot([x_center - 0.15, x_center + 0.15], [upper_whisker, upper_whisker], pen=pg.mkPen(self.theme_colors['spectrum']))
            self.boxPlot.plot([x_center - 0.15, x_center + 0.15], [lower_whisker, lower_whisker], pen=pg.mkPen(self.theme_colors['spectrum']))

            # Outliers (se existirem)
            if len(outliers) > 0:
                self.boxPlot.plot(
                    np.ones(len(outliers)) * x_center,
                    outliers,
                    pen=None,
                    symbol='o',
                    symbolBrush=self.theme_colors['accent'],
                    symbolSize=5
                )
        
        if y_values:
            self.boxPlot.setYRange(min(y_values) - 1, max(y_values) + 1)

        # Mantém os box plots visíveis
        self.boxPlot.autoRange()

    def box_plot_statistics(self, data: list):
        """
        Calcula as estatísticas básicas para o box plot.

        Args:
            data (list): Lista de valores numéricos.
        Returns:
            tuple: (Q1, Q2, Q3, lower_whisker, upper_whisker, outliers)
            
        """
        data = np.array(data)
        # quartis
        q1 = np.percentile(data, 25)
        q2 = np.percentile(data, 50)
        q3 = np.percentile(data, 75)

        # whiskers (bigodes)
        lower_whisker = np.min(data[data >= q1 - 1.5 * (q3 - q1)])
        upper_whisker = np.max(data[data <= q3 + 1.5 * (q3 - q1)])
        # outliers
        outliers = data[(data < lower_whisker) | (data > upper_whisker)]
        return q1, q2, q3, lower_whisker, upper_whisker, outliers

    def clear_plot(self):
        """
        Limpa o gráfico do espectro e do plot temporal.

        """
        self.spectraPlotWidget.clear()
        self.spectraPlotWidget.addItem(self.roi_region)
        self.temporalPlotWidget.clear()
        self.temporalPlotWidget.addItem(self.temporal_roi_region)
        self.results_df = self._empty_result_store()
        self.pending_hdf5 = self._empty_result_store()
        self._clear_fbg_peak_colors()
        # spectraPlotWidget.clear() já removeu os itens; apenas descarta as referências.
        self._peak_marker_lines.clear()
        self.spectra_data = []
        self.roi_range = None
        self.fixed_traces = {}
        self._sync_fix_buttons_ui()
        self._save_active_channel_state()
        self._refresh_merge_views()
        logger.info("Gráficos limpos.")

    def _prompt_sample_name(self) -> tuple[str, bool]:
        """
        Abre diálogo para entrada do nome da amostra com sugestões.
        As sugestões são baseadas nas amostras já carregadas do arquivo aberto.
        Returns:
            tuple: (nome_da_amostra, ok) ou ("", False) se o usuário cancelar a entrada.

        """
        dialog = QInputDialog(self)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setInputMode(QInputDialog.TextInput)
        dialog.setWindowTitle("Nome da Amostra")
        dialog.setLabelText("Insira o nome da amostra para salvar os dados:")

        line_edit = dialog.findChild(QLineEdit)
        if line_edit is not None and self.samples:
            completer = QCompleter(list(self.samples.keys()), dialog)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            line_edit.setCompleter(completer)

        if dialog.exec() != QDialog.Accepted:
            return "", False

        return dialog.textValue(), True

    def save_data(self):
        """
        Abre um diálogo para selecionar um arquivo. Se o arquivo já existir,
        anexa os novos dados; caso contrário, cria um novo arquivo.
        Os dados são filtrados pela ROI temporal antes de serem salvos.
        
        """
        # --- Passo 1: Validação inicial ---
        if not self.results_df['Timestamp']:
            logger.warning("Tentativa de salvar sem dados processados.")
            QMessageBox.warning(self, "Atenção", "Não há dados processados para salvar.")
            return

        logger.info("Iniciando processo para salvar dados.")

        # --- Passo 2: Abre diálogo para inserir o nome da amostra ---
        sample_name, ok = self._prompt_sample_name()

        if not ok:
            return # Usuário cancelou a entrada do nome da amostra

        # --- Passo 3: Obtém o caminho do arquivo do usuário ---
        file_path = self.file_path or self.config_data.get('path')
        if file_path is not None:
            logger.info(f"Usando caminho de arquivo pré-configurado: {file_path}")
        else:
            file_path = prompt_save_file(self)

            if not file_path:
                logger.info("Operação de salvamento cancelada pelo usuário.")
                return

        # --- Passo 4: Processa e filtra os dados da ROI ---
        try:
            self._save_active_channel_state()
            inter = self.config_data.get('inter')
            current_state = self.channel_states.get(self.active_channel_idx, self._default_channel_state())

            save_entries: list[tuple[int | None, dict]] = []
            if self._is_braggmeter() and isinstance(current_state.get('trace_states'), dict):
                for trace_number in sorted(current_state['trace_states'].keys()):
                    trace_state = current_state['trace_states'].get(trace_number)
                    if trace_state is not None:
                        save_entries.append((trace_number, trace_state))
            else:
                save_entries.append((None, current_state))

            total_saved = 0
            for trace_number, state in save_entries:
                timestamps = np.asarray(state['Timestamp'], dtype=np.float64)
                intensities = state['Intensidade']
                trace_suffix = f" - Trace {trace_number}" if trace_number is not None else ""
                sample_name_to_use = f"{sample_name}{trace_suffix}"

                if self._fiber_mode() in ('FBG', 'INT'):
                    peak_values = state['Picos']
                    timestamps_filtered = timestamps
                    intensities_filtered = np.asarray(intensities, dtype=np.float32)
                    resonant_filtered = peak_values

                    if not self._flatten_peak_values(peak_values):
                        feature_plural = 'vales' if self._fiber_mode() == 'INT' else 'picos'
                        logger.warning("Nenhum %s detectado para salvar.", 'vale' if self._fiber_mode() == 'INT' else 'pico')
                        QMessageBox.warning(self, "Atenção", f"Não há {feature_plural} detectados para salvar.")
                        continue
                else:
                    roi_min_ts, roi_max_ts = self.temporal_roi_region.getRegion()
                    valleys = np.asarray(state['Vale'], dtype=np.float64)

                    mask = (timestamps >= roi_min_ts) & (timestamps <= roi_max_ts)

                    timestamps_filtered = timestamps[mask]
                    intensities_filtered = np.asarray(
                        [intensities[i] for i in range(len(intensities)) if mask[i]],
                        dtype=np.float32
                    )
                    resonant_filtered = valleys[mask]

                    if not timestamps_filtered.any():
                        logger.warning("A região selecionada não contém dados para salvar.")
                        QMessageBox.warning(self, "Atenção", "A região selecionada não contém dados para salvar.")
                        continue

                logger.debug(f"Salvando {len(intensities_filtered)} espectros{trace_suffix}")

                append_samples(
                    range_cfg=self.config_data.get('range'),
                    res=self.config_data.get('res'),
                    file_path=file_path,
                    inter=inter,
                    intensities=intensities_filtered,
                    timestamps=timestamps_filtered,
                    values=resonant_filtered,
                    sample_name=sample_name_to_use,
                    dataset_name=self._result_key(),
                )

                total_saved += len(timestamps_filtered)

                if self._fiber_mode() in ('FBG', 'INT'):
                    sample_peak_boxes = self._fbg_current_peak_samples(
                        timestamps_filtered.tolist(),
                        resonant_filtered,
                        label_prefix="Pico" if self._fiber_mode() == 'FBG' else "Vale",
                        feature_label="Pico" if self._fiber_mode() == 'FBG' else "Vale",
                    )
                    if sample_peak_boxes:
                        self.samples[sample_name_to_use] = sample_peak_boxes
                    else:
                        resonant_for_box = self._from_meter(self._flatten_peak_values(resonant_filtered), self.resultUnit)
                        self.samples[sample_name_to_use] = self.box_plot_statistics(resonant_for_box)
                else:
                    resonant_for_box = self._from_meter(resonant_filtered, self.resultUnit)
                    self.samples[sample_name_to_use] = self.box_plot_statistics(resonant_for_box)

            if total_saved == 0:
                return

            logger.info(f"Dados salvos com sucesso em: {file_path}")
            QMessageBox.information(self, "Sucesso", f"{total_saved} medições foram salvas com sucesso em:\n{file_path}")

            # Armazena o caminho definido para futuro uso
            self.file_path = file_path
            self.setWindowTitle(f"Análise de dados - {os.path.basename(file_path)}")
            self.samples = dict(reversed(self.samples.items())) # Mantém a ordem de inserção (última amostra salva aparece primeiro)
            self._save_active_channel_state()
            self._save_persistent_settings()

        except Exception as e:
            logger.error(f"Falha ao processar ou salvar os dados: {e}")
            QMessageBox.critical(self, "Erro", f"Ocorreu um erro ao salvar o arquivo:\n{e}")

    def load_file(self, file_path: str):
        """
        Carrega dados de amostras de um arquivo HDF5 para análise.

        Args:
            file_path (str): Caminho do arquivo HDF5 contendo os dados das amostras.
            
        """
        try:
            inter = self.config_data.get('inter')
            data = load_samples(file_path, inter)

            for sample_name, sample_payload in data.items():
                dataset_name = sample_payload.get('dataset')
                sample_values = sample_payload.get('values', [])

                if dataset_name == 'Picos':
                    feature_label = "Vale" if self._fiber_mode() == 'INT' else "Pico"
                    # Reconstrói séries para separar boxplots por pico recorrente da mesma amostra
                    peak_series = [[float(v) for v in np.asarray(row, dtype=float).ravel().tolist()] for row in sample_values]
                    timestamps = list(range(len(peak_series)))
                    grouped = self._recurring_fbg_peak_groups(timestamps, peak_series, min_count=2)

                    if grouped:
                        sample_peak_boxes = []
                        for i, (_, points) in enumerate(grouped.items(), start=1):
                            values_m = [wl for _, wl in points]
                            values_unit = self._from_meter(values_m, self.resultUnit)
                            sample_peak_boxes.append((f"{feature_label} {i}", self.box_plot_statistics(values_unit)))
                        self.samples[sample_name] = sample_peak_boxes
                    else:
                        flattened = self._flatten_peak_values(peak_series)
                        sample_wavelengths = self._from_meter(np.asarray(flattened, dtype=float), self.resultUnit)
                        self.samples[sample_name] = self.box_plot_statistics(sample_wavelengths)
                else:
                    sample_wavelengths = np.asarray(sample_values, dtype=float)
                    sample_wavelengths = self._from_meter(sample_wavelengths, self.resultUnit)
                    self.samples[sample_name] = self.box_plot_statistics(sample_wavelengths)

            if not self.samples:
                raise ValueError("Nenhum dado de amostra encontrado no arquivo.")
            
            self.update_box_plot() # Atualiza os gráficos com os box plots carregados
            self.setWindowTitle(f"Análise de dados - {os.path.basename(file_path)}")
            self.file_path = file_path
            self._save_active_channel_state()
            self._save_persistent_settings()
            logger.info(f"Dados carregados com sucesso do arquivo: {file_path}. {len(self.samples)} amostra(s) encontrada(s).")

        except Exception as e:
            logger.error(f"Erro ao carregar o arquivo {file_path}: {e}")
            QMessageBox.critical(self, "Erro", f"Não foi possível carregar o arquivo:\n{e}")

    def continuous_cfg(self) -> bool:
        """
        Configura a aquisição contínua de dados.
        Abre diálogos para o usuário inserir o nome da amostra e o caminho de salvamento.
        A coleta ocorre continuamente até o usuário parar manualmente.
        Os dados são salvos automaticamente a cada X amostras ou X tempo para segurança.
        Returns:
            bool: True se a configuração foi concluída com sucesso, False se o usuário cancelou em alguma etapa.
        
        """
        # Abre diálogo para inserir o nome da amostra ---
        self.sample_name, ok = self._prompt_sample_name()

        if not ok:
            return False # Usuário cancelou a entrada do nome da amostra

        # Obtém o caminho do arquivo do usuário ---
        file_path = prompt_save_file(self) if self.file_path is None else self.file_path

        if not file_path:
            logger.info("Operação de salvamento cancelada pelo usuário.")
            return False
        
        # Atualiza o caminho de salvamento para o modo contínuo
        self.file_path = file_path
        if self.enabled_channels:
            for idx in self.enabled_channels:
                self.channel_states[idx]['file_path'] = file_path
        
        # Atualiza o título da janela para exibir o arquivo aberto
        self.setWindowTitle(f"Análise de dados - {os.path.basename(file_path)}")
        
        logger.debug(f"Caminho de salvamento definido para o modo contínuo: {file_path}")
        logger.info(f"Amostra contínua '{self.sample_name}' iniciada. Clique em 'Parar' para interromper. Os dados serão salvos automaticamente.")

        self._save_active_channel_state()
        seeded_state = self._empty_result_store()
        for key in seeded_state:
            seeded_state[key] = list(self.results_df.get(key, []))
        self.pending_hdf5 = seeded_state
        current_state = self.channel_states.get(self.active_channel_idx)
        if current_state is not None:
            current_state['pending_hdf5'] = self.pending_hdf5

        self._save_active_channel_state()
        self._save_persistent_settings()
        return True
        
    def closeEvent(self, event):
        """
        Sobrescreve o evento de fechar a janela.

        Em vez de fechar a aplicação, esta função emite um sinal 'closing'
        para que a janela de configuração possa reaparecer.
        
        """
        self._save_persistent_settings()
        self._cleanup_thread()
        super().closeEvent(event)
        if isinstance(self.config_data, dict):
            self.closing.emit(self.config_data.get('theme', self.theme))
        else:
            self.closing.emit(self.theme)