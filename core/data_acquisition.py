from PySide6.QtCore import Signal, QObject, QThread

import logging
import time
from serial.serialutil import SerialException

from core.hardware import Imon512, BraggMeter, ThorLabsCCT, ThorLabs, MultiSercaloSwitch

logger = logging.getLogger(__name__)

class DataAcquisition(QObject):
    """
    Classe responsável pela aquisição de dados dos dispositivos IMON512 e FS22DI.

    Args:
        inter (str): Modelo da interface.
        ip (str): Endereço IP do dispositivo (se aplicável).
        port (str): Porta de comunicação (número da porta serial ou TCP/IP, se aplicável).
        osa: Instância compartilhada de PyCCT/OSA para evitar conflito de múltiplas instâncias (se aplicável).
        switch_ports (list[str], opcional): Lista de portas dos switches Sercalo (se detectados) - pode haver múltiplos.

    """
    # Sinal para indicar que novos dados foram adquiridos.
    # Usa object para evitar conversões rígidas C++.
    data_acquired = Signal(object, object, int) # spectrum, warn, channel
    # Sinal para indicar que a aquisição foi finalizada
    finished = Signal()
    # Sinal para indicar erro (para mostrar mensagem na thread principal)
    error_occurred = Signal(str, str)  # title, message
    # Sinal para iniciar a thread de aquisição após a configuração ocorrer sem erros
    start_thread = Signal()

    def __init__(self, inter: str, ip: str, port: str, osa, switch_ports: list[str] | None = None):
        super().__init__()
        # Inicializa o dispositivo como None
        self.device: object | None = None
        self.switch: MultiSercaloSwitch | None = None
        self._stopping = False
        self._paused = False

        # Inicializa os parâmetros de porta e interface
        self.inter = inter
        self.ip = ip
        self.port = port
        self.osa = osa
        self.switch_ports = switch_ports if switch_ports else []
        
        # Flags para modo contínuo (OSA203 com múltiplos canais)
        self._continuous_mode = False
        self._spectrum_averaging = 1
        self._last_switch_channel: int | None = None

    def run(self):
        """
        Inicia a aquisição de dados com base na interface selecionada.
        
        Para OSA203 com múltiplos canais, inicia modo contínuo para evitar
        pausas entre aquisições de canais diferentes.

        """
        self._stopping = False
        self._paused = False
        self._last_switch_channel = None

        if hasattr(self, 'device') and self.device is not None:
            self.stop()
            
        try:
            match self.inter:
                case 'IBSEN IMON-512':
                    self.device = Imon512(port=self.port)
                case 'BRAGGMETER FS22DI':
                    self.device = BraggMeter(self.ip, int(self.port), True)
                case 'BRAGGMETER FS22DI HBM':
                    self.device = BraggMeter(self.ip, int(self.port), False)
                case 'THORLABS CCT11':
                    self.device = ThorLabsCCT(cct=self.osa)
                case 'THORLABS OSA203':
                    self.device = ThorLabs(osa=self.osa)
                    self._continuous_mode = True
                case _:
                    logger.error(f"Interface desconhecida: {self.inter}")
                    self.error_occurred.emit("Erro", f"Interface desconhecida: {self.inter}")
                    self.finished.emit()
                    return
            if self.switch_ports:
                self.switch = MultiSercaloSwitch(self.switch_ports)

            if self._continuous_mode:
                self.device.start_continuous_acquisition(spectrum_averaging=self._spectrum_averaging)
                logger.info("Modo de aquisição contínua ativado para OSA203")
        except PermissionError as e:
            logger.error(f"Permissão negada ao abrir porta {self.port}. {e}")
            self.error_occurred.emit("Erro", f"Permissão negada ao abrir porta {self.port}. Certifique-se que a porta não está em uso.")
            self.finished.emit()
            self.device = None
            return
        except Exception as e:
            logger.error(f"Erro ao inicializar o {self.inter}: {e}")
            self.error_occurred.emit("Erro", f"Falha ao inicializar o {self.inter}.")
            self.finished.emit()
            self.device = None
            return
        self.start_thread.emit()

    def stop(self):
        """
        Encerra a aquisição de dados e fecha a conexão com o dispositivo.

        """
        # Se já está parando e não há mais recursos, nada a fazer.
        if self._stopping and self.device is None and self.switch is None:
            return

        self._stopping = True
        device = self.device
        switch = self.switch
        self.device = None
        self.switch = None
        self._last_switch_channel = None

        try:
            logger.info(f"Fechando conexão com {self.inter}.")
            if device is not None:
                device.stop_continuous_acquisition()
            if switch is not None:
                switch.close()
            if device is not None:
                device.close()
            logger.info("Conexão fechada.")
        except AttributeError:
            logger.debug("Dispositivo já estava fechado ou não inicializado.")
        except Exception as e:
            logger.error(f"Erro ao fechar dispositivo: {e}")
        self.finished.emit()

    def request_data(self, n_mean: int, channel: int, bragg_traces: list[bool]):
        """
        Solicita um novo conjunto de dados do dispositivo.
        
        Se houver múltiplos switches, sincroniza todos para o mesmo canal
        e valida que todos foram alterados corretamente.
        
        Args:
            n_mean (int): Número de amostras para média espectral.
            channel (int): Canal a ser lido do switch.
        """
        if self._stopping:
            return

        if self._paused:
            return

        if QThread.currentThread().isInterruptionRequested():
            return

        device = self.device
        switch = self.switch

        if not hasattr(self, 'device') or device is None:
            return

        try:
            is_bragg_with_switch = isinstance(device, BraggMeter) and switch is not None
            channel_switched = (
                is_bragg_with_switch
                and self._last_switch_channel is not None
                and self._last_switch_channel != channel
            )

            if switch is not None:
                # Em modo contínuo (OSA203), apenas sincroniza sem pausas
                if self._continuous_mode:
                    # Para evitar espectros mistos, a troca de canal acontece
                    # com a aquisição contínua completamente parada.
                    device.stop_continuous_acquisition()

                    # Em modo contínuo, ainda validamos que os dois switches
                    # chegaram ao mesmo canal antes de reiniciar a leitura.
                    max_retries = 3
                    current_channel = -1
                    for _ in range(max_retries):
                        switch.set_channel(channel)
                        try:
                            current_channel = switch.get_channel()
                        except Exception:
                            current_channel = -1

                        if current_channel == channel:
                            break

                    if current_channel != channel:
                        raise Exception(
                            f"Falha ao sincronizar canal {channel} em todos os switches Sercalo."
                        )

                    # Reinicia a aquisição apenas depois da confirmação total
                    # da troca de canal.
                    device.start_continuous_acquisition(spectrum_averaging=self._spectrum_averaging)
                    device.set_channel_info(channel)
                else:
                    # Modo padrão (não contínuo): aguarda switch estar estável
                    switch.set_channel(channel)
                    time.sleep(0.05)
                    # Valida que todos os switches foram alterados corretamente
                    max_retries = 3
                    cur_channel = -1
                    for _ in range(max_retries):
                        cur_channel = switch.get_channel()
                        if cur_channel == channel:
                            break
                        switch.set_channel(channel)
                        time.sleep(0.05) # Pequena pausa para retry

                    if cur_channel != channel:
                        raise Exception(f"Falha ao configurar canal {channel} em todos os switches Sercalo.")

            # Para BraggMeter com switch: ao alternar entre canais, descarta uma
            # aquisição dummy imediatamente após o settle para evitar vazamento.
            if channel_switched:
                self._discard_bragg_post_switch_dummy(device, bragg_traces)

            if switch is not None:
                self._last_switch_channel = channel

            spectrum, warn = device.get_osa_trace(n_mean, bragg_traces)
            if spectrum is None:
                if self._paused:
                    # Silent return if paused - this is expected behavior during pause
                    return
                logger.debug("Espectro vazio retornado (pode estar pausado ou desconectado).")
                self.data_acquired.emit([], warn, channel)

            if not self._stopping:
                self.data_acquired.emit(spectrum, warn, channel)
        except SerialException as e:
            if self._stopping:
                return
            logger.error(f"Dispositivo desconectado: {e}", exc_info=True)
            self.error_occurred.emit("Erro de Comunicação", f"A conexão com o dispositivo na porta {self.port} foi perdida.")
            self.stop()
            return
        except Exception as e:
            if self._stopping:
                return
            logger.error(f"Ocorreu um erro durante a execução: {e}", exc_info=True)
            self.error_occurred.emit("Erro inesperado", str(e))
            self.stop()
            return

    def _discard_bragg_post_switch_dummy(self, device: BraggMeter, bragg_traces: list[bool]):
        """
        Executa uma aquisição dummy após troca de canal do switch para BraggMeter.

        A primeira leitura após chaveamento pode conter resquício do canal anterior,
        por isso a leitura é descartada antes da aquisição efetiva.
        """
        if self._stopping or self._paused:
            return

        try:
            device.get_osa_trace(1, bragg_traces)
            logger.debug("Aquisição dummy do BraggMeter descartada após troca de canal do switch.")
        except Exception as e:
            # Não interrompe o ciclo por falha no dummy: tenta a leitura efetiva.
            logger.warning(f"Falha ao descartar aquisição dummy do BraggMeter: {e}")

    def get_fast_traces(self, n: int):
        """
        Retorna os traces rápidos do IMON para análise da FFT.

        """
        if self._stopping:
            return

        if self._paused:
            return

        if QThread.currentThread().isInterruptionRequested():
            return

        device = self.device

        if not hasattr(self, 'device') or device is None:
            return
        
        if self.inter != 'IBSEN IMON-512':
            return

        try:
            spectrum, warn = device.get_multiple_osa_traces(n)
            if spectrum is not None and not self._stopping:
                self.data_acquired.emit(spectrum, warn, 0)
            elif spectrum is None and self._paused:
                # Silent return if paused - this is expected behavior during pause
                return
            elif spectrum is None:
                logger.debug("Espectro vazio retornado (pode estar pausado ou desconectado).")
        except SerialException as e:
            if self._stopping:
                return
            logger.error(f"Dispositivo desconectado: {e}", exc_info=True)
            self.error_occurred.emit("Erro de Comunicação", f"A conexão com o dispositivo na porta {self.port} foi perdida.")
            self.stop()
            return
        except Exception as e:
            if self._stopping:
                return
            logger.error(f"Ocorreu um erro durante a execução: {e}", exc_info=True)
            self.error_occurred.emit("Erro inesperado", str(e))
            self.stop()
            return

    def pause(self):
        """
        Pausa temporariamente a aquisição sem fechar o dispositivo.

        """
        self._paused = True
        if self._continuous_mode and self.device is not None:
            try:
                self.device.stop_continuous_acquisition()
            except Exception as e:
                logger.warning(f"Falha ao pausar aquisição contínua: {e}")

    def resume(self):
        """
        Retoma a aquisição após uma pausa.

        """
        if not self._stopping:
            if self._continuous_mode and self.device is not None:
                try:
                    self.device.start_continuous_acquisition(spectrum_averaging=self._spectrum_averaging)
                except Exception as e:
                    logger.error(f"Falha ao retomar aquisição contínua: {e}")
                    raise
            self._paused = False

    def set_exposure_time(self, et: float):
        """
        Altera o tempo de exposição.
        Args:
            et (float): Novo tempo de exposição.
            
        """
        if not self._stopping and hasattr(self, 'device') and self.device is not None:
            self.device.set_exposure_time(et)
            logger.info(f"Tempo de exposição alterado para {et}.")

    def get_exposure_time(self) -> int:
        """
        Returns:
            int: o tempo de exposição atual.

        """
        if not self._stopping and hasattr(self, 'device') and self.device is not None:
            et = self.device.get_exposure_time()
            logger.info(f"Tempo de exposição atual: {et}.")
            return et
        return 0

    def set_spectrum_averaging(self, n_mean: int):
        """
        Configura o número de espectros para média espectral.

        Args:
            n_mean (int): Número de espectros para média.
        """
        self._spectrum_averaging = n_mean

        # Se em modo contínuo, reinicia com novo averaging
        if self._continuous_mode and hasattr(self, 'device') and self.device is not None:
            try:
                self.device.stop_continuous_acquisition()
                self.device.start_continuous_acquisition(spectrum_averaging=self._spectrum_averaging)
                logger.info(f"Spectrum averaging alterado para {self._spectrum_averaging} em modo contínuo")
            except Exception as e:
                logger.error(f"Erro ao alterar spectrum averaging: {e}")