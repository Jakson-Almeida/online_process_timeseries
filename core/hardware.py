import numpy as np
import logging
import socket
import serial
import time
import re

logger = logging.getLogger(__name__)

class BraggMeter:
    """
    Represents a BraggMeter object used for controlling and retrieving data from a BraggMeter device.

    Args:
        host (str): The IP address of the BraggMeter device. Default is '10.0.0.150'.
        port (int): The port number of the BraggMeter device. Default is 3500.

    Attributes:
        commands (dict): A dictionary containing the commands used to communicate with the BraggMeter device.
        host (str): The IP address of the BraggMeter device.
        port (int): The port number of the BraggMeter device.
        timeout (int): The timeout value for the socket connection.
        sock (socket.socket): The socket connection object.

    Raises:
        RuntimeError: If the BraggMeter device is in the heating state.

    """

    def __init__(self, host='10.0.0.150', port: int=3500, legacy_cmds=False):
        """
        Initializes a new instance of the BraggMeter class.

        Args:
            host (str): The IP address of the BraggMeter device. Default is '10.0.0.150'.
            port (int): The port number of the BraggMeter device. Default is 3500.
            legacy_cmds (bool): If True, uses legacy commands (for old BraggMeter - FiberSensing). Default is False.

        Raises:
            RuntimeError: If the BraggMeter device is in the heating state.

        """
        self.legacy_cmds = legacy_cmds
        if self.legacy_cmds:
            self.commands = {'status': 	"\000\000\000\006:STAT?\r\n".encode('ascii'),
                            'start': 	"\000\000\000\n:ACQU:STAR\n".encode('ascii'),
                            'stop': 	"\000\000\000\n:ACQU:STOP\n".encode('ascii'),
                            'trace0': 	"\000\000\000\023:ACQU:OSAT:CHAN:0\n?".encode('ascii'),
                            'trace1': 	"\000\000\000\023:ACQU:OSAT:CHAN:1\n?".encode('ascii'),
                            'trace2': 	"\000\000\000\023:ACQU:OSAT:CHAN:2\n?".encode('ascii'),
                            'trace3': 	"\000\000\000\023:ACQU:OSAT:CHAN:3\n?".encode('ascii'),
                            }
        else:
            self.commands = {'status': ":STAT?\r\n".encode('ascii'),
                            'start':  ":ACQU:STAR\r\n".encode('ascii'),
                            'stop':   ":ACQU:STOP\r\n".encode('ascii'),
                            'trace0': ":ACQU:OSAT:CHAN:0?\r\n".encode('ascii'),
                            'trace1': ":ACQU:OSAT:CHAN:1?\r\n".encode('ascii'),
                            'trace2': ":ACQU:OSAT:CHAN:2?\r\n".encode('ascii'),
                            'trace3': ":ACQU:OSAT:CHAN:3?\r\n".encode('ascii'),
                            'bragg0': ":ACQU:WAVE:CHAN:0?\r\n".encode('ascii'),
                            'bragg1': ":ACQU:WAVE:CHAN:1?\r\n".encode('ascii'),
                            'bragg2': ":ACQU:WAVE:CHAN:2?\r\n".encode('ascii'),
                            'bragg3': ":ACQU:WAVE:CHAN:3?\r\n".encode('ascii'),
                            'power0': ":ACQU:POWE:CHAN:0?\r\n".encode('ascii'),
                            'power1': ":ACQU:POWE:CHAN:1?\r\n".encode('ascii'),
                            'power2': ":ACQU:POWE:CHAN:2?\r\n".encode('ascii'),
                            'power3': ":ACQU:POWE:CHAN:3?\r\n".encode('ascii'),
                            'gain0?': ":ACQU:CONF:GAIN:CHAN:0?\r\n".encode('ascii'),
                            'gain1?': ":ACQU:CONF:GAIN:CHAN:1?\r\n".encode('ascii'),
                            'gain2?': ":ACQU:CONF:GAIN:CHAN:2?\r\n".encode('ascii'),
                            'gain3?': ":ACQU:CONF:GAIN:CHAN:3?\r\n".encode('ascii'),
                            }
        self.host = host
        self.port = port
        self.timeout = 5
        self.sock = None

        self.open()
        self.start()
        time.sleep(0.05)

    def __del__(self):
        """
        Destructor for the BraggMeter class. Ensures that the socket connection is closed when the object is destroyed.
        """
        self.close()

    def close(self):
        """
        Closes the socket connection to the BraggMeter device.
        """
        if self.sock is not None:
            try:
                self.sock.close()
                logger.info('Successfuly closed socket.')
            except PermissionError as e:
                logger.debug(f'Permission error closing socket on {self.host}:{self.port}, ignoring: {e}')
            except Exception as e:
                logger.warning(f'Error closing socket: {e}')
            finally:
                self.sock = None

    def open(self):
        """
        Opens a socket connection to the BraggMeter device.
        """
        for attempt in range(3):
            try:
                if self.sock is not None:
                    # logger.debug('Closing existing socket before reopening.')
                    try:
                        self.sock.close()
                    except (socket.error, AttributeError):
                        pass
                    self.sock = None

                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                self.sock.connect((self.host, self.port))
                # logger.debug(f'Successfully opened socket to {self.host}:{self.port}')
                time.sleep(0.05) # Small delay to ensure connection is fully established before sending commands
                return
            except Exception as e:
                self.sock = None
                if attempt < 2:
                    logger.warning(
                        f'Failed to open socket to {self.host}:{self.port} on attempt {attempt + 1}/2: {e}. Retrying...')
                    time.sleep(0.1)
                else:
                    logger.error(f'Failed to open socket to {self.host}:{self.port}: {e}')
                    raise ConnectionError(f'Failed to open socket: {e}')

    def ask(self, key):
        """
        Sends a command to the BraggMeter device and returns the response.

        Args:
            key (str): The key corresponding to the command to be sent.

        Returns:
            str: The response from the BraggMeter device.

        """
        string = self.commands[key]
        resp = self.send(string)
        return resp

    def flush(self, sock, timeout=0.01):
        sock.setblocking(False)
        start = time.time()
        try:
            while time.time() - start < timeout:
                try:
                    data = sock.recv(4096)
                    if not data:
                        break
                except BlockingIOError:
                    time.sleep(0.001)
        finally:
            sock.setblocking(True)

    def send(self, string, retries=0):
        """
        Assure a socket connection to the BraggMeter device, sends a command, and returns the response.

        Args:
            string (str): The command to be sent.
            retries (int): Number of retry attempts.

        Returns:
            str: The response from the BraggMeter device.

        """
        try:
            self.flush(self.sock)
            self.sock.sendall(string)
            resp = b''
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
                if b'\n' in chunk:
                    break
            resp = resp.decode('latin-1')
        except (socket.error, socket.timeout) as e:
            logger.error(f'Socket error: {e}. Retrying...')
            if retries > 2: # 3 attempts total
                raise ConnectionError(f'Max retries exceeded: {e}')
            self.open()
            return self.send(string, retries=retries+1)
        return resp

    def start(self):
        """
        Starts the acquisition process on the BraggMeter device.

        """
        status = self.get_status()
        if status == 1:
            self.ask('start')
        elif status == 3 or status == 4:
            self.ask('stop')
            self.ask('start')
        elif status == 5:
            err_msg = 'BraggMeter em aquecimento.'
            logger.error(err_msg)
            raise RuntimeError(err_msg)

    def stop(self):
        """
        Stops the acquisition process on the BraggMeter device.

        Returns:
            str: The response from the BraggMeter device.

        """
        resp = self.ask('stop')
        status = self.get_status()
        logger.info(f'BraggMeter status: {status}')

        return resp

    def get_status(self):
        """
        Retrieves the status of the BraggMeter device.

        Returns:
            int: The status code of the BraggMeter device.

        """
        resp = self.ask('status')
        logger.debug(f'Resposta do status: {resp}')
        resp = resp.split(':')
        loc = 0
        for i in range(0, len(resp)):
            if resp[i] == 'ACK':
                loc = i + 1
        return int(resp[loc])

    def get_osa_trace(self, n_mean: int, bragg_traces: list[bool], *args):
        """
        Retrieves the OSA trace data from the specified channel.

        Args:
            n_mean (int): The number of samples to average.
            bragg_traces (list[bool]): A list indicating which Bragg traces to include.

        Returns:
            numpy.ndarray: An array containing the wavelength and trace data.  
            str: An warning message if the spectrum is saturated, otherwise None.
        """

        traces = [i for i, include in enumerate(bragg_traces) if include]
        try:
            warn = []
            trace_wavelengths = []
            trace_powers = []
            spectrum = []
            
            for trace in traces:
                warn_raw = False
                wl_raw = []
                power_raw = []
                spec_count = 0

                while spec_count < n_mean:
                    for retry in range(5):
                        resp = self.ask(f'trace{trace}')
                        if resp:
                            break
                        self.open()
                    else:
                        raise ConnectionError(f'No response received for trace{trace} after 5 attempts.')

                    # Try to isolate payload after ACK if present
                    ack_idx = resp.find('ACK')
                    payload = resp[ack_idx+4:] if ack_idx != -1 else resp
                    payload = payload.strip().strip('\r\n')
                    parts = payload.split(':') if payload else []

                    if self.legacy_cmds:
                        pot = parts[-1] if parts else ''
                        # legacy pot is comma separated numeric values
                        trace_vals = re.findall(r'[-+]?[0-9]*\.?[0-9]+', pot)
                        try:
                            power_raw.append(np.array([float(x) for x in trace_vals], dtype=float))
                        except ValueError:
                            logger.debug('Invalid float values in BraggMeter response')
                            continue
                        wl_raw.append(np.linspace(1500, 1600, len(power_raw[-1])))
                    else:
                        if len(parts) < 2:
                            logger.debug('Incomplete response from BraggMeter (no data/wavelength part)')
                            return None, None
                        pot, wl_str = parts[-2], parts[-1]
                        # remove any non-hex characters and join contiguous hex chunks
                        hex_chunks = re.findall(r'[0-9A-Fa-f]+', pot)
                        pot_hex = ''.join(hex_chunks)
                        # split into 3-char hex words
                        hex_values = [pot_hex[i:i+3] for i in range(0, len(pot_hex), 3) if pot_hex[i:i+3]]
                        if not hex_values:
                            logger.debug('No hex values parsed from BraggMeter response')
                            return None, None
                        try:
                            power_raw.append(np.array([int(hv, 16) for hv in hex_values], dtype=float))
                        except ValueError:
                            logger.debug('Invalid hex values in BraggMeter response')
                            return None, None
                        wl_vals = re.findall(r'[-+]?[0-9]*\.?[0-9]+', wl_str)
                        wl_raw.append(np.array([float(x) for x in wl_vals], dtype=float))

                    if len(power_raw[-1]) == 0 or len(wl_raw[-1]) == 0:
                        logger.debug('Parsed empty wavelength or trace from BraggMeter')
                        return None, None

                    # Ensure both arrays have the same dimension by truncating to the smaller
                    min_len = min(
                        min(len(w) for w in wl_raw),
                        min(len(p) for p in power_raw)
                    )

                    for i, (w, p) in enumerate(zip(wl_raw, power_raw)):
                        if len(w) != min_len or len(p) != min_len:
                            logger.debug(f'Wavelength and trace length mismatch (wl={len(wl_raw[-1])},' 
                                         f'trace={len(power_raw[-1])}). Truncating to {min_len}.')

                    wl_raw = [w[:min_len] for w in wl_raw]
                    power_raw = [p[:min_len] for p in power_raw]

                    warn_raw = bool(np.max(power_raw[-1]) == 4095) or warn_raw
                    
                    spec_count += 1
                    time.sleep(0.02) # Small delay between measurements

                trace_wavelengths.append(np.mean(wl_raw, axis=0))
                trace_powers.append(np.mean(power_raw, axis=0))
                warn.append(warn_raw)

            for i in range(len(traces)):
                spec = np.stack((trace_wavelengths[i], trace_powers[i]), axis=1)
                spectrum.append(np.flipud(spec))
            return spectrum, warn
    
        except Exception as e:
            logger.error(f'Error reading trace from BraggMeter: {e}')
            raise e

    def get_peaks(self, channel):
        """
        Retrieves the peak intensity from the specified channel.

        Args:
            channel (int): The channel number.

        Returns:
            list: A list of peak intensities.

        """
        if self.legacy_cmds:
            logger.error('get_peaks not implemented for legacy commands')
            return -1
        try:
            lambdas = self.ask(f'power{channel}')
        except Exception as e:
            logger.error(f'Error reading power from BraggMeter: {e}')
            self.start()
            lambdas = self.ask(f'power{channel}')
        i = lambdas.find('ACK') + 4
        lambdas = lambdas[i:-2].split(',')
        if len(lambdas) == 0:
            return []
        if lambdas[0] == '':
            return []
        return [float(lamb) if lamb else 0 for lamb in lambdas]
    
    def get_bragg(self, channel):
        """
        Retrieves the peak wavelength from the specified channel.

        Args:
            channel (int): The channel number.

        Returns:
            list: A list of peak positions.

        """
        if self.legacy_cmds:
            logger.error('get_bragg not implemented for legacy commands')
            return -1
        try:
            lambdas = self.ask(f'bragg{channel}')
        except Exception as e:
            logger.error(f'Error reading bragg from BraggMeter: {e}')
            self.start()
            lambdas = self.ask(f'bragg{channel}')
        i = lambdas.find('ACK') + 4
        lambdas = lambdas[i:-2].split(',')
        if len(lambdas) == 0:
            return []
        if lambdas[0] == '':
            return []
        return [float(lamb) if lamb else 0 for lamb in lambdas]
    
    def set_gain(self, channel, gain):
        """
        Sets the gain for the specified channel.

        Args:
            channel (int): The channel number.
            gain (str): The gain value to be set from 0 to 255.

        Returns:
            str: The response from the BraggMeter device.

        """
        if self.legacy_cmds:
            logger.error('set_gain not implemented for legacy commands')
            return -1
        
        command = f":ACQU:CONF:GAIN:CHAN:{channel}:{gain}\r\n".encode('ascii')
        resp = self.send(command)
        return resp
    
    def set_threshold(self, channel, threshold):
        """
        Sets the threshold for the specified channel.

        Args:
            channel (int): The channel number.
            threshold (str): The threshold value to be set from 200 to 3200.

        Returns:
            str: The response from the BraggMeter device.

        """
        if self.legacy_cmds:
            logger.error('set_threshold not implemented for legacy commands')
            return -1
        command = f" :ACQU:CONF:THRE:CHAN:{channel}:{threshold}\r\n".encode('ascii')
        resp = self.send(command)
        return resp

    def get_gain(self, channel):
        """
        Retrieves the gain value for the specified channel.

        Args:
            channel (int): The channel number.

        Returns:
            str: The gain value.

        """
        if self.legacy_cmds:
            logger.error('get_gain not implemented for legacy commands')
            return -1
        resp = self.ask(f'gain{channel}?')
        return resp

class Imon512:
    """
    Represents an Imon512 object used for controlling and retrieving data from an Imon512 device.

    Args:
        port (str): The port name of the Imon512 device. Default is 'COM5'.
        baudrate (int): The baud rate of the Imon512 device. Default is 921600.

    Attributes:
        port (str): The port name of the Imon512 device.
        baudrate (int): The baud rate of the Imon512 device.
        serial_port (serial.Serial): The serial port connection object.

    """

    def __init__(self, port='COM5', baudrate=921600):
        self.port = port
        self.baudrate = baudrate

        self.serial_port = None
        self.open()
        
        try:
            for _ in range(1): # 2 attempts
                self.ask('*idn?')
                response = self.listen()
                response = response.decode(errors='ignore').strip()
                logger.debug(response)
                if response == 'JETI_VersaPIC_RU60':
                    time.sleep(0.1)
                    break
                time.sleep(0.1)
            else:
                raise ConnectionError(f'Unexpected device response after opening port: {response}')
        except Exception as e:
            logger.error(f'Communication error after opening port: {e}')
            raise e

        # A, B1, B2, ..., B5
        self.wl_param = self.update_coefficients()
        # alpha, alpha0, beta, beta0
        self.tem_param = self.update_temperature_coefficients()
        self.wl = np.arange(0, 510, dtype=float)
        self.temp = None
        self.set_format(5) # Little endian binary output

    def close(self):
        """
        Closes the serial port connection safely.
        """
        if self.serial_port is not None:
            try:
                # Tenta fechar sem verificar is_open para evitar acessar handle inválido
                self.serial_port.close()
                logger.info(f'Successfully closed port {self.port}')
            except OSError as e:
                # Ignora erros de handle inválido (porta já foi fechada ou desconectada)
                if hasattr(e, 'winerror') and e.winerror in [6, 9]:  # ERROR_INVALID_HANDLE ou outros
                    logger.debug(f'Port {self.port} handle already invalid, ignoring: {e}')
                else:
                    logger.warning(f'Error closing port {self.port}: {e}')
            except (AttributeError, TypeError) as e:
                # Handle já foi destruído ou está None
                logger.debug(f'Port {self.port} already destroyed: {e}')
            except Exception as e:
                logger.warning(f'Unexpected error closing port {self.port}: {e}')
            finally:
                self.serial_port = None

    def open(self):
        """
        Opens the serial port connection with retry logic.
        """
        for attempt in range(3):
            try:
                # Fecha conexão existente antes de abrir uma nova
                if self.serial_port is not None:
                    logger.debug(f'Closing existing connection on {self.port} before reopening.')
                    try:
                        if hasattr(self.serial_port, 'is_open'):
                            self.serial_port.close()
                    except (OSError, AttributeError):
                        pass # Ignora erros ao verificar/fechar porta
                    self.serial_port = None

                self.serial_port = serial.Serial(port=self.port, baudrate=self.baudrate, timeout=1)
                logger.debug(f'Successfully opened port {self.port}')
                return
            except Exception as e:
                self.serial_port = None  # Garante que serial_port está None em caso de erro
                if attempt < 2:
                    logger.warning(f'Failed to open port {self.port} on attempt {attempt + 1}/3: {e}. Retrying...')
                    time.sleep(0.1)
                else:
                    logger.error(f'Failed to open port {self.port}: {e}')
                    raise e

    def listen(self):
        """
        Listens for the response from the Imon512 device.

        Returns:
            bytes: The response from the Imon512 device.

        """
        self.serial_port.reset_input_buffer()
        response = self.serial_port.readline()
        return response

    def set_format(self, format: int):
        """
        Sets the output format (0-7)
        
        """
        self.ask('*para:form 5')

    def get_temperature(self):
        """
        Retrieves the temperature data from the Imon512 device.

        Returns:
            float: The temperature value.

        """
        self.ask('temperature?')
        response = self.listen()
        response = response.decode().strip()
        self.temp = float(response)
        return self.temp

    def get_wavelength(self):
        """
        Retrieves the wavelength data from the Imon512 device.

        Returns:
            numpy.ndarray: An array containing the wavelength data.

        """
        return self.wl

    def ask(self, command: str):
        """
        Send command to Imon512.

        Args:
            command (str): The command to be sent.
        """
        retries = 2
        string = (command + '\r').encode()

        for attempt in range(retries):
            if self.serial_port is None or not self.serial_port.is_open:
                logger.warning('Serial port is not open.')
                try:
                    self.open()
                except Exception as e:
                    logger.warning(f'Failed to open port {self.port} on attempt {attempt + 1}/{retries}: {e}')
                    time.sleep(0.1)
                    continue

            try:
                self.serial_port.reset_input_buffer()
                self.serial_port.reset_output_buffer()
            except (serial.SerialException, OSError, AttributeError) as e:
                logger.warning(f'Failed to flush buffers on {self.port}: {e}. Reopening port...')
                self.close()
                time.sleep(0.05)
                continue

            try:
                self.serial_port.write(string)
                return True
            except (serial.SerialException, AttributeError, OSError) as e:
                if self.serial_port is not None:
                    logger.warning(
                        f'Write failed on {self.port} (attempt {attempt + 1}/{retries}): {e}')
                    self.close()
                    time.sleep(0.1)

        logger.error(f'Unable to send command to {self.port} after {retries} attempts: {command}')
        return False

    def update_coefficients(self):
        """
        Reads the wavelength fit coefficients from unit flash memory.
        Updates the wl_param attribute.
        """
        self.ask('*rdusr2 0')
        resp = self.listen()

        wl_param = [None]*6
        for i in range(0,6):
            wl_param[i] = float(resp[(i*16):((i+1)*16)])
        return wl_param

    def update_temperature_coefficients(self):
        """
        Reads the temperature compensation coefficients from unit flash memory.
        Updates the tem_param attribute.
        """
        self.ask(f'*rdusr2 1')
        resp = self.listen()

        tem_param = [None]*4
        for i in range(0,4):
            tem_param[i] = float(resp[(i*16):((i+1)*16)])
        return tem_param

    def fit_wavelength(self, n_pix):
        """
        Fit pixels to wavelength.
        Updates the wavelength attribute.

        Args:
            n_pix (int): The number of pixels.
        Returns:
            numpy.ndarray: An array containing the fitted wavelength data.

        """
        pix = np.arange(0, n_pix, dtype=float)
        wl = np.zeros_like(pix)
        for n, coef in enumerate(self.wl_param):
            wl += coef * pix ** float(n)
        return self.temperature_compensation(wl)

    def temperature_compensation(self, wl: np.ndarray):
        """
        Compensates the wavelength data for temperature.
        Args:
            wl (numpy.ndarray): The wavelength data to be compensated.
        Returns:
            numpy.ndarray: An array containing the temperature-compensated wavelength data.

        """
        temp = self.temp
        self.ask('*meas:temper')
        try:
            response = self.listen()
            decoded = response.decode(errors='ignore')
            logger.debug(f'Temp response: {decoded}')
            if 'Temperature:' not in decoded:
                logger.debug('Temperature unavailable; skipping wavelength compensation.')
                return wl
            try:
                temp = float(decoded.split(':')[-1].split('\r')[0].strip())
                self.temp = temp
            except (ValueError, IndexError) as e:
                logger.warning(f'Failed to parse temperature response, using cached value: {e}')

            return (wl - self.tem_param[2] * temp - self.tem_param[2]) \
                      / (1 + self.tem_param[0] * temp + self.tem_param[1])
        except Exception as e:
            logger.error(f'Failed to compensate for temperature: {e}')        

    def measure(self, n_mean=1, return_single=True):
        """
        Measure the spectrum.
        
        Args:
            n_mean (int): The number of measurements to be averaged.
            return_single (bool): If True, returns the mean of the measurements, otherwise returns all measurements.
        
        Returns:
            numpy.ndarray: The measured data.
            bool: Pixel count exceed the saturation threshold.
        """
        n_pix = 510
        bytes_per_spec = 2 * n_pix
        
        measurements = []
        warn = False
        self.wl = self.fit_wavelength(510)
        self.ask('*meas:fstmeas')
        spec_count = 0
        leftover = b''
        while spec_count < n_mean:
            # Verifica se a porta e seus objetos internos estão válidos
            if (self.serial_port is not None and hasattr(self.serial_port, 'is_open')):
                in_waiting = self.serial_port.in_waiting
                if in_waiting > 0: # Se houver dados disponíveis
                    try:
                        new_data = self.serial_port.read(size=in_waiting) # Lê tudo que tiver disponível
                        chunk = leftover + new_data
                        parts = chunk.split(b'\x00\x00') # Procura o marcador de sincronismo (Pixel 1)
                        # O primeiro elemento de parts é lixo antes do primeiro Sync
                        # Os elementos intermediários que têm o tamanho correto são espectros
                        for i in range(len(parts) - 1):
                            spec = parts[i]
                            if len(spec) == (bytes_per_spec - 2): # -2 porque o split removeu o 0x0000
                                # Reconstroi o dado (adicionando o zero de volta se necessário)
                                spec = np.frombuffer(b'\x00\x00' + spec, dtype='<u2') # '<u2' para little-endian
                                warn = warn or bool(np.max(spec) > 48800)
                                measurements.append(spec[1:])
                                spec_count += 1

                        # Guarda o que sobrou para a próxima leitura
                        leftover = b'\x00\x00' + parts[-1] if chunk.endswith(parts[-1]) else b""

                        # Proteção para não estourar a memória se algo der muito errado
                        if len(leftover) > bytes_per_spec * 2:
                            leftover = b""
                            logging.warning('Leftover buffer exceeded expected size, clearing to prevent memory issues.')
                    except (AttributeError, OSError, TypeError) as e:
                        raise ValueError(f'Read error in measurement: {e}.')
            else:
                logger.error('Serial port or internal objects became invalid during measurement')
        self.ask('esc')
        measurements = np.array(measurements, dtype=float)
        if return_single:
            spectrum = np.stack((self.wl[1::], np.mean(measurements, axis=0)), axis=1)
            return np.flipud(spectrum), warn
        else:
            spectrum = np.stack(([self.wl[1::]]*len(measurements), measurements), axis=1)
            return np.flipud(spectrum), warn
    
    def get_osa_trace(self, n_mean: int=1, return_single: bool=True, *args):
        """
        Retrieves the OSA trace data.
        
        Returns:
            numpy.ndarray: The OSA trace data.
            str: An warning message if the spectrum is saturated, otherwise None.
        """
        spec, warn = self.measure(n_mean=n_mean, return_single=return_single)
        warn = None if not warn else "Pixel count exceed the saturation threshold."
        return spec, warn
    
    def get_multiple_osa_traces(self, n_mean: int=1):
        """
        Retrieves multiple OSA trace data.
        
        Returns:
            numpy.ndarray: The OSA trace data.
            str: An warning message if the spectrum is saturated, otherwise None.
        """
        spec, warn = self.measure(n_mean=n_mean, return_single=False)
        warn = None if not warn else "Pixel count exceed the saturation threshold."
        return spec, warn

    def set_exposure_time(self, et: int):
        """
        Sets the exposure time of the Imon512 device.

        Args:
            et (int): The exposure time in microseconds.

        """  
        self.ask(f'*para:fftpara 3000 {et} 0')
        logger.debug(f'Set integration time to {et} µs')

    def get_exposure_time(self) -> int:
        """
        Gets the exposure time of the Imon512 device.

        Returns:
            int: The exposure time in microseconds.

        """  
        self.ask('*para:fftpara?')
        response = self.listen()
        response = response.decode().strip()
        et = response.split('\t')[2]
        et = int(et.split('\r')[0])
        return et

class ThorLabsCCT:
    """
    Bridge class to access pyCCT and interact with Thorlabs Compact Spectrograph SDK.
    
    """

    def __init__(self, cct):
        self.cct = cct
        devices = self.cct.discover_devices()
        if not devices:
            raise RuntimeError("No Thorlabs Compact Spectrograph devices found.")
        try:
            self.device = self.cct.connect_to_device(devices[0])
        except Exception as e:
            raise RuntimeError(f"Failed to connect to device: {e}")

    def close(self):
        """
        Cleans up the device instance.
        """
        self.device = None

    def get_osa_trace(self, n_mean: int=1, *args):
        """
        Retrieves the OSA trace data from the Thorlabs Compact Spectrograph.

        Returns:
            numpy.ndarray: The OSA trace data.
            str: An warning message if the spectrum is saturated, otherwise None.

        """
        if self.device is None:
            raise RuntimeError("Device not connected.")

        try:
            err = not self.device.set_hardware_average(n_mean)
            resp = self.device.acquire_single_spectrum()
            wl, intensities, exp_meas, ave_meas = resp
            spec = np.stack((wl, intensities), axis=1)

            warn = None if not self.device.is_saturated() else "Spectrum is saturated."
            if err:
                warn = "Failed to set hardware average." if not warn else warn + " Failed to set hardware average."
            return spec, warn
        except Exception as e:
            raise RuntimeError(f"Failed to acquire spectrum: {e}")

    def get_exposure_time(self) -> int:
        """
        Gets the exposure time of the Thorlabs Compact Spectrograph.

        Returns:
            int: The exposure time in microseconds.

        """  
        if self.device is None:
            raise RuntimeError("Device not connected.")
        try:
            et = self.device.get_manual_exposure() * 1e3 # Convert from ms to µs
            return et
        except Exception as e:
            raise RuntimeError(f"Failed to get exposure time: {e}")

    def set_exposure_time(self, et: float):
        """
        Sets the exposure time of the Thorlabs Compact Spectrograph.

        Args:
            et (float): The exposure time in microseconds.

        """  
        if self.device is None:
            raise RuntimeError("Device not connected.")
        try:
            self.device.set_manual_exposure(et * 1e-3)  # Convert from µs to ms
            logger.info(f"Exposure time set to {et} µs.")
        except Exception as e:
            raise RuntimeError(f"Failed to set exposure time: {e}")

class ThorLabs:
    """
    Class to access pyOSA and interact with Thorlabs OSA 20X.
    
    O fluxo para OSA203 é exclusivamente contínuo.
    """

    def __init__(self, osa):
        self.device = osa
        self.warming_error = False
        self._continuous_iterator = None
        self._continuous_active = False
        self._current_channel_info = {
            'channel': None,
            'sequence': 0,
            'switch_time': None,
        }
        try:
            self.device.setup(autogain=False)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize device: {e}")

    def close(self):
        """
        Cleans up the device instance.
        """
        if self._continuous_active:
            self.device.stop = True
            self._continuous_active = False
            self._continuous_iterator = None
        self.device = None

    def start_continuous_acquisition(self, spectrum_averaging: int = 1):
        """
        Inicia aquisição contínua mantendo fluxo sem interrupções.
        
        Args:
            spectrum_averaging (int): Número de espectros para média.
        """
        if self._continuous_active:
            return
        
        try:
            # Small delay to ensure FTSLib is completely idle before restarting
            import time
            time.sleep(0.1)
            
            self._continuous_iterator = self.device.acquire_continuous(
                spectrum=True,
                interferogram=False,
                spectrum_averaging=spectrum_averaging,
                apodization="None",
                y_unit="dBm",
                ignore_errors=["Reference Warmup"]
            )
            self._continuous_active = True
            logger.debug("Aquisição contínua iniciada para OSA203")
        except Exception as e:
            logger.error(f"Erro ao iniciar aquisição contínua: {e}")
            raise RuntimeError(f"Failed to start continuous acquisition: {e}")

    def stop_continuous_acquisition(self):
        """
        Para a aquisição contínua COMPLETAMENTE e finaliza o FTSLib.
        
        Isso consome a iteração final do acquire_continuous() para garantir
        que __stop_continuous_acq() seja chamado e a aquisição FTSLib termine.
        """
        if self._continuous_active:
            if self._continuous_iterator is not None:
                # Signal the iterator to stop
                self.device.stop = True
                
                # Give callbacks a moment to check the stop flag
                import time
                time.sleep(0.05)
                
                # Drain any queued data from callbacks that fired before stop
                if hasattr(self.device, '_drain_queues'):
                    try:
                        self.device._drain_queues()
                    except Exception as e:
                        logger.debug(f"Error draining queues: {e}")
                
                # Consume the final iteration to allow __stop_continuous_acq() to be called
                try:
                    next(self._continuous_iterator)
                except StopIteration:
                    # Expected - the iterator is exhausted after stop=True
                    logger.debug("Iterator properly exhausted after stop signal")
                except Exception as e:
                    logger.warning(f"Exception consuming final iterator (may still have stopped FTSLib): {e}")
            
            self._continuous_active = False
            self._continuous_iterator = None
            logger.debug("Aquisição contínua parada completamente para OSA203")

    def flush_continuous_readout(self):
        """
        Remove espectros pendentes da fila interna do pyOSA sem parar a aquisição.
        """
        if self.device is None:
            return
        if hasattr(self.device, '_drain_queues'):
            self.device._drain_queues()
            logger.debug("Fila de leitura contínua drenada para OSA203")

    def set_channel_info(self, channel: int):
        """
        Registra mudança de canal para sincronizar com espectros contínuos.
        
        Args:
            channel (int): Número do canal que será ativo.
        """
        self._current_channel_info['channel'] = channel
        self._current_channel_info['sequence'] += 1
        self._current_channel_info['switch_time'] = time.time()
        logger.debug(f"Canal {channel} registrado para sincronização (seq: {self._current_channel_info['sequence']})")

    def _get_continuous_spectrum(self):
        """
        Retorna o próximo espectro do iterador contínuo.
        
        Returns:
            tuple: (wavelengths, intensities, warn_message)
        """
        if not self._continuous_active or self._continuous_iterator is None:
            raise RuntimeError("Aquisição contínua não está ativa")
        
        try:
            # Get next spectrum from continuous acquisition
            resp = next(self._continuous_iterator)
            spectrum_data = None
            if isinstance(resp, dict):
                spectrum_data = resp.get("spectrum")
                if spectrum_data is None:
                    spectrum_data = resp.get(("spectrum", "Detector 1"))
            else:
                try:
                    spectrum_data = resp["spectrum"]
                except Exception:
                    for key in resp.keys():
                        if isinstance(key, tuple) and key and key[0] == "spectrum":
                            spectrum_data = resp[key]
                            break
            
            if spectrum_data is None:
                raise RuntimeError("No spectrum data in continuous acquisition")
            
            spectrum = spectrum_data
            wl = np.asarray(spectrum.get_x(), dtype=float)
            intensities = np.asarray(spectrum.get_y(), dtype=float)
            
            if not intensities.any() or wl is None:
                raise RuntimeError("No valid spectrum acquired.")
            
            # Check spectrum validity
            validity = spectrum.check_validity()
            warn_messages = []
            if not validity["ref_laser_locked"]:
                warn_messages.append("Reference laser not locked")
            if not validity["interferogram_within_detector_range"]:
                warn_messages.append("Interferogram is clipped")
            if not validity["interferogram_is_linear"]:
                warn_messages.append("Interferogram is non-linear")
            if not validity["autogain_satisfied"]:
                warn_messages.append("Autogain was not finished adjusting")
            
            warn = ", ".join(warn_messages) if warn_messages else None
            
            return wl, intensities, warn
        except StopIteration:
            # Iterator stopped - likely due to pause() consuming final iteration
            logger.debug("Continuous acquisition iterator exhausted (normal during pause)")
            return None, None, None
        except Exception as e:
            logger.error(f"Erro ao obter espectro contínuo: {e}")
            raise RuntimeError(f"Failed to get continuous spectrum: {e}")

    def get_osa_trace(self, n_mean: int=1, *args):
        """
        Retrieves the OSA trace data from the Thorlabs OSA 20X.

        Retorna o próximo espectro da aquisição contínua.

        Returns:
            numpy.ndarray: The OSA trace data (wl, intensity pairs).
            str: An warning message if the spectrum has validity issues, otherwise None.
        """
        if self.device is None:
            raise RuntimeError("Device not connected.")

        try:
            # If continuous acquisition is not active, return None
            # This can happen if pause() was called while a request_data was in flight
            if not self._continuous_active:
                logger.debug("get_osa_trace called but continuous acquisition not active (likely paused)")
                return None, None

            wl, intensities, warn = self._get_continuous_spectrum()
            
            # If _get_continuous_spectrum returned None (iterator exhausted), return None
            if wl is None or intensities is None:
                return None, None
                
            spec = np.stack((wl, intensities), axis=1)
            return spec, warn
        except Exception as e:
            logger.error(f"Failed to acquire spectrum: {e}")
            return None, None
        
class SercaloSwitch:
    """
    Class to control Sercalo switch via serial and handle the acquisitions as a intermediary.
    
    """

    def __init__(self, port='COM3', baudrate=115200):
        self.port = port
        self.baudrate = baudrate
        self.serial_port = None
        self.open()

        try:
            self.ask(':iden?')
            if not self.ack():
                raise RuntimeError('Sercalo switch not found.')
        except Exception as e:
            logger.error(f'Communication error after opening port: {e}')
            raise e

    def close(self):
        if self.serial_port is not None:
            try:
                self.serial_port.close()
                logger.info(f'Successfully closed port {self.port}')
            except Exception as e:
                logger.warning(f'Error closing port {self.port}: {e}')
            finally:
                self.serial_port = None

    def open(self):
        for attempt in range(3):
            try:
                if self.serial_port is not None:
                    logger.debug(f'Closing existing connection on {self.port} before reopening.')
                    try:
                        self.serial_port.close()
                    except (OSError, AttributeError):
                        pass
                    self.serial_port = None

                self.serial_port = serial.Serial(port=self.port, baudrate=self.baudrate, timeout=1)
                logger.debug(f'Successfully opened port {self.port}')
                return
            except Exception as e:
                self.serial_port = None
                if attempt < 2:
                    logger.warning(f'Failed to open port {self.port} on attempt {attempt + 1}/3: {e}. Retrying...')
                    time.sleep(0.1)
                else:
                    logger.error(f'Failed to open port {self.port}: {e}')
                    raise e

    def ask(self, command: str):
        retries = 1
        if self.serial_port is None or not self.serial_port.is_open:
            logger.warning("Serial port is not open.")
            self.open()
        try:
            full_command = f'{command}\r\n'
            self.serial_port.write(full_command.encode())

        except (serial.SerialException, AttributeError, OSError) as e:
            logger.error(f'Write failed on {self.port}: {e}. Retrying...')
            if retries > 2:
                raise ConnectionError(f'Max retries exceeded on write: {e}')
            self.ask(command, retries)

    def ack(self):
        timeout = 2
        retries = 2
        timeout += time.monotonic()

        for attempt in range(retries):
            while time.monotonic() < timeout:
                if self.serial_port is None or not self.serial_port.is_open:
                    return -1
                response = self.serial_port.readline().decode(errors="ignore").strip()
                if response:
                    if ':ack' in response:
                        return True
                    elif ':nack' in response:
                        return False

            if attempt < retries:
                logger.warning(
                    f'No response received from {self.port} within {timeout}s. '
                    f'Retrying {attempt + 1}/{retries}...')
                try:
                    self.serial_port.reset_input_buffer()
                except (serial.SerialException, OSError, AttributeError):
                    pass
                timeout += time.monotonic()

        raise TimeoutError(
            f'No response received on Sercalo switch on {self.port} after {retries} attempt(s).')

    def set_channel(self, channel: int):
        command = f':set-chan-{channel}'
        self.ask(command)
        if not self.ack():
            raise RuntimeError(f"Failed to set channel: {channel}")
        
    def get_channel(self):
        retries = 2
        for attempt in range(retries):
            self.ask(':get-chan?')
            timeout = time.monotonic() + 3.0 # 3 seconds timeout
            response = []

            while time.monotonic() < timeout:
                if self.serial_port is None or not self.serial_port.is_open:
                    raise RuntimeError("Serial port is not open.")

                response.append(self.serial_port.readline().decode(errors="ignore").strip())
                if not response:
                    continue

                if ':ack' in response:
                    try:
                        channel_line = next(
                            line for line in reversed(response)
                            if line and ':ack' not in line and ':nack' not in line)
                        channel_str = channel_line.split(':')[-1].split(',')[0].strip()
                        return int(channel_str)
                    except StopIteration:
                        raise RuntimeError(f'Invalid channel response: {response}')
                    except ValueError as e:
                        logger.error(f'Failed to parse channel from response: {e}')
                        raise RuntimeError(f'Invalid channel response: {response}')

                if ':nack' in response:
                    logger.warning(f'Failed to get channel on attempt {attempt + 1}/{retries}: {response}')
                    break

            if attempt < retries:
                time.sleep(0.05)

        raise RuntimeError(f'Failed to get channel after {retries} attempt(s).')


class MultiSercaloSwitch:
    """
    Class to control multiple Sercalo switches simultaneously and ensure they are all commanded at the same time.

    """

    def __init__(self, ports: list[str]):
        """
        Initialize multiple Sercalo switches on the specified COM ports.
        
        Args:
            ports (list[str]): list of COM port for the Sercalo switches
        
        """
        if not ports:
            raise RuntimeError('No switch ports provided.')
        
        self.ports = ports
        self.switches: list[SercaloSwitch] = []
        
        # Inicializa todos os switches
        for port in ports:
            try:
                switch = SercaloSwitch(port=port)
                self.switches.append(switch)
                logger.info(f'Switch Sercalo initialized on port {port}')
            except Exception as e:
                # Fecha todos os switches já inicializados em caso de erro
                self.close()
                raise RuntimeError(f'Failed to initialize switch on port {port}: {e}')
    
    def close(self):
        """
        Close all Sercalo switches.

        """
        for switch in self.switches:
            try:
                switch.close()
            except PermissionError as e:
                logger.debug(f'Permission error closing switch on {switch.port}, ignoring: {e}')
            except Exception as e:
                logger.warning(f'Error closing switch: {e}')
        self.switches = []
    
    def set_channel(self, channel: int):
        """
        Define the channel on all switches simultaneously.
        
        Args:
            channel (int): Number of the channel to be configured
        
        """
        for switch in self.switches:
            try:
                switch.set_channel(channel)
            except Exception as e:
                raise RuntimeError(f'Failed to configure channel {channel} on switch {switch.port}: {e}')
    
    def get_channel(self):
        """
        Gets the channel from the first switch and validates that all are on the same channel.
        
        Returns:
            int: Number of the current channel
        
        """
        if not self.switches:
            raise RuntimeError('No switches available.')
        
        channels = []
        for switch in self.switches:
            try:
                if switch.serial_port is not None and switch.serial_port.is_open:
                    channel = switch.get_channel()
                    channels.append((switch.port, channel))
            except Exception as e:
                raise RuntimeError(f'Failed to get channel from switch {switch.port}: {e}')
        
        # Check if all channels are the same
        first_channel = channels[0][1]
        for port, channel in channels:
            if channel != first_channel:
                mismatched = [f'{p}: {c}' for p, c in channels]
                raise RuntimeError(
                    f'Switches unsynchronized. Channels detected: {", ".join(mismatched)}')

        return first_channel


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)