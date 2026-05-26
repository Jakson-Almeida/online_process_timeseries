# -*- coding: utf-8 -*-

"""
Módulo de backend para processamento de dados espectrais.

Este arquivo contém funções puras de cálculo, independentes da interface
gráfica, para analisar os espectros dos sensores.
"""

import logging
import numpy as np
from scipy.optimize import curve_fit
from scipy.signal import savgol_filter, windows, find_peaks

# Configura o logger para este módulo
logger = logging.getLogger(__name__)


def find_resonant_wavelength(wavelengths: np.ndarray,
                             power: np.ndarray,
                             roi_min: float,
                             roi_max: float) -> float | None:
    """
    Encontra o comprimento de onda ressonante (mínimo de potência) em uma ROI.

    Args:
        wavelengths (np.ndarray): Array com os comprimentos de onda.
        power (np.ndarray): Array com os valores de potência.
        roi_min (float): Limite inferior da Região de Interesse (ROI).
        roi_max (float): Limite superior da Região de Interesse (ROI).

    Returns:
        [float | None]: O valor do comprimento de onda ressonante encontrado,
                         ou None se nenhum dado estiver presente na ROI.

    """
    try:
        # 1. Cria uma máscara booleana para filtrar os dados dentro da ROI.
        # Esta é a forma mais eficiente de selecionar dados em numpy.
        mask = (wavelengths >= roi_min) & (wavelengths <= roi_max)

        # 2. Aplica a máscara para obter apenas os dados da região de interesse.
        roi_wavelengths = wavelengths[mask]
        roi_power = power[mask]

        # 3. Verifica se existe algum dado na ROI selecionada.
        if len(roi_wavelengths) == 0:
            logger.warning("Nenhum ponto de dados encontrado na ROI especificada.")
            return None

        # 4. Encontra o ÍNDICE do menor valor de potência na ROI.
        min_power_index = np.argmin(roi_power)

        # 5. Usa o índice para encontrar o COMPRIMENTO DE ONDA correspondente.
        resonant_wavelength = roi_wavelengths[min_power_index]

        pop, _ = curve_fit(lorentz, roi_wavelengths, roi_power, 
                           p0=[-10, resonant_wavelength, 100, max(power), 0],
                           bounds=([-np.inf, roi_min, 1, -np.inf, -1], [0, roi_max, np.inf, np.inf, 1]))
        resonant_wavelength = pop[1]

        return resonant_wavelength

    except Exception as e:
        logger.error(f"Ocorreu um erro ao buscar o comprimento de onda ressonante: {e}")
        return None

def lorentz(x: np.ndarray, a: float, x0: float, w: float, bias: float, incl: float) -> np.ndarray:
    """
    Calcula o valor de uma função Lorentziana em um determinado ponto.

    Esta função modela um pico Lorentziano, frequentemente usado para
    ajustar curvas em espectroscopia.

    Args:
        x (np.ndarray): Ponto(s) de entrada onde a função será calculada.
        a (float): Amplitude ou escala da função (altura do pico).
        x0 (float): Posição do centro do pico (média, Mu).
        w (float): Largura à meia altura (FWHM - Full Width at Half Maximum, Gama).
        bias (float): Deslocamento vertical (offset no eixo Y).
        incl (float): Inclinação linear (tendência linear no eixo Y).
    Returns:
        np.ndarray: O valor da função Lorentziana calculado em 'x'.

    """
    return a * (1 + ((x - x0) / (w / 2)) ** 2) ** (-1) + bias + incl * (x - x0)

def find_wavelength_peaks(wavelengths: np.ndarray,
                           power: np.ndarray,
                           prominence: float = None,
                           distance: int = None,
                           width: float = None,
                           valley: bool = False,
                           fit_model: str = 'gaussian') -> np.ndarray | None:
    """
    Encontra picos/vales em uma curva de potência e ajusta um modelo
    (gaussiano ou lorentziano) para estimar os comprimentos de onda correspondentes.

    Args:
        wavelengths (np.ndarray): Array com os comprimentos de onda.
        power (np.ndarray): Array com os valores de potência.
        prominence (float, optional): Prominência mínima dos picos a serem encontrados.
        distance (int, optional): Distância mínima entre picos.
        width (float, optional): Largura mínima dos picos a serem encontrados.
        valley (bool, optional): Quando True, detecta vales (mínimos locais) usando a
            curva invertida internamente.
        fit_model (str, optional): Modelo usado no ajuste local. Valores aceitos:
            'gaussian' ou 'lorentz'.

    Returns:
        np.ndarray | None: Array com os índices dos picos encontrados, ou None se nenhum pico for encontrado.
    
    """
    try:
        # -------- 1. DETECÇÃO --------
        signal_for_detection = -power if valley else power
        peaks, props = find_peaks(
            signal_for_detection,
            prominence=prominence,
            distance=distance,
            width=width
        )

        if len(peaks) == 0:
            return None

        # -------- 2. ORDENA POR IMPORTÂNCIA --------
        order = np.argsort(props["prominences"])[::-1]
        peaks = peaks[order]

        results = []

        delta_lambda = wavelengths[1] - wavelengths[0]

        # -------- 3. PROCESSA CADA PICO --------
        for i, idx in enumerate(peaks):
            try:
                if "widths" in props: # largura estimada, se disponível
                    half_window = int(props["widths"][order[i]] * 2)
                else:
                    half_window = int(0.5 / delta_lambda)  # fallback (~0.5 nm)

                start = max(0, idx - half_window)
                end   = min(len(power), idx + half_window)

                x_local = wavelengths[start:end]
                y_local = power[start:end]

                if len(x_local) < 5:
                    logger.debug(f"Pico {i}: insuficientes pontos ({len(x_local)}) para ajuste")
                    continue

                # -------- CHUTE INICIAL --------
                x0 = wavelengths[idx]

                if fit_model == 'lorentz':
                    a0 = (np.min(y_local) - np.max(y_local)) if valley else (np.max(y_local) - np.min(y_local))
                    w0 = max((x_local[-1] - x_local[0]) / 3, np.finfo(float).eps)
                    bias0 = np.median(y_local)
                    incl0 = 0.0
                    pop, pcov = curve_fit(
                        lorentz,
                        x_local,
                        y_local,
                        p0=[a0, x0, w0, bias0, incl0],
                        bounds=([
                            -np.inf if valley else 0,
                            x_local[0],
                            np.finfo(float).eps,
                            -np.inf,
                            -np.inf,
                        ], [
                            0 if valley else np.inf,
                            x_local[-1],
                            np.inf,
                            np.inf,
                            np.inf,
                        ]),
                        maxfev=5000
                    )
                    x0_fit = pop[1]
                    width_fit = pop[2]
                    amplitude_fit = pop[0]
                    y_fit = lorentz(x_local, *pop)
                    fwhm = width_fit
                else:
                    a0 = np.max(y_local) - np.min(y_local)
                    x0 = wavelengths[idx]
                    sigma0 = max((x_local[-1] - x_local[0]) / 6, np.finfo(float).eps)
                    bias0 = np.min(y_local)
                    pop, pcov = curve_fit(
                        gaussian,
                        x_local,
                        y_local,
                        p0=[a0, x0, sigma0, bias0],
                        bounds=([0, x_local[0], np.finfo(float).eps, -np.inf],
                                [np.inf, x_local[-1], np.inf, np.inf]),
                        maxfev=5000
                    )
                    x0_fit = pop[1]
                    width_fit = pop[2]
                    amplitude_fit = pop[0]
                    y_fit = gaussian(x_local, *pop)
                    fwhm = 2.355 * width_fit

                # -------- VALIDAÇÃO --------
                # Verifica se os parâmetros estão dentro de bounds razoáveis
                if width_fit <= 0:
                    logger.debug(f"Pico {i}: largura ajustada inválida ({width_fit})")
                    continue

                if not valley and amplitude_fit <= 0:
                    logger.debug(f"Pico {i}: amplitude inválida para pico ({amplitude_fit})")
                    continue
                if valley and amplitude_fit >= 0:
                    logger.debug(f"Pico {i}: amplitude inválida para vale ({amplitude_fit})")
                    continue
                
                if not (x_local[0] <= x0_fit <= x_local[-1]):
                    logger.debug(f"Pico {i}: wavelength ajustado fora da janela local")
                    continue
                
                # Calcula qualidade do fit (RMSE)
                residuals = y_local - y_fit
                rmse = np.sqrt(np.mean(residuals ** 2))
                y_range = np.max(y_local) - np.min(y_local)
                
                if y_range > 0:
                    relative_rmse = rmse / y_range
                    # Rejeita fit com RMSE relativo muito alto (fit pobre)
                    if relative_rmse > 0.5:
                        logger.debug(f"Pico {i}: qualidade de fit insuficiente (rel_rmse={relative_rmse:.3f})")
                        continue
                
                results.append({
                    "wavelength": x0_fit,
                    "fwhm": fwhm,
                    "amplitude": amplitude_fit,
                    "fit_quality": 1.0 - relative_rmse if y_range > 0 else 1.0
                })

            except RuntimeError as e:
                logger.debug(f"Falha na convergência do fit para pico {i}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Falha no fit do pico {i}: {e}")
                continue

        # -------- 4. ORDENA POR COMPRIMENTO DE ONDA --------
        results = sorted(results, key=lambda r: r["wavelength"])

        if results:
            logger.debug(f"Detectados {len(results)} picos com curve_fit adequado")
        else:
            logger.warning("Nenhum pico com curve_fit adequado foi encontrado")

        return results if results else None

    except Exception as e:
        logger.error(f"Erro ao buscar picos: {e}")
        return None

def gaussian(x: np.ndarray, a: float, x0: float, sigma: float, bias: float) -> np.ndarray:
    """
    Calcula o valor de uma função Gaussiana em um determinado ponto.

    Esta função modela um pico Gaussiano, frequentemente usado para
    ajustar curvas em espectroscopia.

    Args:
        x (np.ndarray): Ponto(s) de entrada onde a função será calculada.
        a (float): Amplitude ou escala da função (altura do pico).
        x0 (float): Posição do centro do pico (média, Mu).
        sigma (float): Desvio padrão, que controla a largura do pico.
    Returns:
        np.ndarray: O valor da função Gaussiana calculado em `x`.
    
    """
    return a * np.exp(-((x - x0) ** 2) / (2 * sigma ** 2)) + bias

def _apodize_plot_data(y_values: np.ndarray, methods: dict, apodization: str | None = None) -> np.ndarray:
    """
    Aplica apodização aos dados de plotagem.

    Args:
        y_values (np.ndarray): Array de valores de potência a serem processados.
        methods (dict): Dicionário mapeando nomes de métodos de apodização para suas funções correspondentes.
        apodization (str | None): Nome do método de apodização a ser aplicado, ou None para não aplicar apodização.

    Returns:
        np.ndarray: Array com os dados de potência apodizados.

    """
    if apodization is None:
        return y_values

    method = methods.get(apodization)
    if method is None:
        return y_values

    try:
        window = method(y_values.size, np.std(y_values))
        window /= np.mean(window)
        y_apodized = y_values * window
        return y_apodized
    except Exception as exc:
        logger.error(f"Erro ao aplicar apodização '{apodization}': {exc}")
        return y_values

def preprocess_plot_data(y_values: np.ndarray, methods: dict, savgol_window_size: int, savgol_polyorder: int, apodization: str | None = None) -> np.ndarray:
    """
    Faz o pré-processamento dos dados de potência para visualização, aplicando apodização e filtro de Savitzky-Golay.
    Args:
        y_values (np.ndarray): Array de valores de potência a serem processados.
        methods (dict): Dicionário mapeando nomes de métodos de apodização para suas funções correspondentes.
        savgol_window_size (int): Tamanho da janela para o filtro de Savitzky-Golay (deve ser ímpar).
        savgol_polyorder (int): Ordem do polinômio para o filtro de Savitzky-Golay.
        apodization (str | None): Nome do método de apodização a ser aplicado, ou None para não aplicar apodização.

    """
    y_values = _apodize_plot_data(y_values, methods, apodization)
    if savgol_window_size > 0:
        # Valida que o tamanho da janela não excede o número de pontos
        # window_length deve ser <= tamanho do sinal para usar interpolação
        y_len = len(y_values)
        effective_window_size = min(savgol_window_size, y_len)
        
        # Garante que a janela é ímpar
        if effective_window_size > 0 and effective_window_size % 2 == 0:
            effective_window_size -= 1
        
        # Garante que polyorder < window_length
        effective_polyorder = min(savgol_polyorder, max(0, effective_window_size - 1))
        
        if effective_window_size > 0 and effective_polyorder >= 0:
            try:
                y_values = savgol_filter(y_values, effective_window_size, effective_polyorder)
            except Exception as e:
                logger.warning(f"Savitzky-Golay filter failed: {e}. Returning unfiltered data.")
                # Retorna dados sem filtro se houver erro
        
    return y_values

def build_peak_track_fft(
    traces: np.ndarray,
    wavelengths_m: np.ndarray | None = None,
    sample_rate_hz: float = 3000.0,
    peak_prominence: float | None = None,
    peak_distance: int | None = None,
    peak_width: float | None = None,
    valley: bool = False,
) -> dict | None:
    """
    Extrai um traço temporal de picos (ou vales) e calcula a FFT em 3 kHz.

    Args:
        traces (np.ndarray): Matriz 2D com um espectro por linha.
        wavelengths_m (np.ndarray | None): Eixo de comprimento de onda em metros.
        sample_rate_hz (float): Taxa de aquisição do traço temporal.
        peak_prominence (float | None): Prominência usada na detecção.
        peak_distance (int | None): Distância mínima entre picos/vales detectados.
        peak_width (float | None): Largura mínima dos picos/vales detectados.
        valley (bool): Quando True, detecta vales em vez de picos.

    Returns:
        dict | None: Payload com o traço temporal e a FFT, ou None se inválido.
    """
    try:
        traces = np.asarray(traces, dtype=float)
        if traces.ndim == 1:
            traces = traces[np.newaxis, :]

        if traces.ndim != 2 or traces.shape[0] == 0 or traces.shape[1] == 0:
            return None

        _, n_points = traces.shape
        if wavelengths_m is None:
            wavelengths = np.arange(n_points, dtype=float)
        else:
            wavelengths = np.asarray(wavelengths_m, dtype=float).ravel()
            if wavelengths.size != n_points:
                logger.warning(
                    "Eixo de comprimentos de onda incompativel com os tracos rapidos: %s != %s",
                    wavelengths.size,
                    n_points,
                )
                return None

        poi_positions: list[float] = []
        poi_amplitudes: list[float] = []

        for spectrum in traces:
            spectrum = np.asarray(spectrum, dtype=float)
            if not np.any(np.isfinite(spectrum)):
                continue

            finite = spectrum[np.isfinite(spectrum)]
            fill_value = float(np.median(finite)) if finite.size else 0.0
            spectrum = np.nan_to_num(spectrum, nan=fill_value, posinf=fill_value, neginf=fill_value)

            signal_for_detection = -spectrum if valley else spectrum
            dynamic_range = float(np.max(signal_for_detection) - np.min(signal_for_detection))
            prominence = peak_prominence if peak_prominence is not None else max(dynamic_range * 0.08, np.finfo(float).eps)
            distance = peak_distance if peak_distance is not None else max(1, n_points // 80)

            peaks, props = find_peaks(
                signal_for_detection,
                prominence=prominence,
                distance=distance,
                width=peak_width,
            )

            if peaks.size == 0:
                poi_index = int(np.argmin(spectrum) if valley else np.argmax(spectrum))
            else:
                prominences = np.asarray(props.get('prominences', []), dtype=float)
                if prominences.size == 0:
                    poi_index = int(peaks[0])
                else:
                    poi_index = int(peaks[int(np.argmax(prominences))])

            poi_positions.append(float(wavelengths[poi_index]))
            poi_amplitudes.append(float(spectrum[poi_index]))

        if len(poi_positions) < 2:
            return None

        peak_track = np.asarray(poi_positions, dtype=float)

        if peak_track.size >= 5:
            window_size = 5
            kernel = np.ones(window_size, dtype=float) / window_size
            padded = np.pad(peak_track, (window_size // 2, window_size // 2), mode='edge')
            smoothed = np.convolve(padded, kernel, mode='valid')
            residual = np.abs(peak_track - smoothed)
            residual_med = float(np.median(residual))
            residual_mad = float(np.median(np.abs(residual - residual_med)))
            threshold = residual_med + 6.0 * residual_mad if residual_mad > 0 else residual_med * 3.0

            if threshold > 0:
                outliers = residual > threshold
                if np.any(outliers) and np.count_nonzero(~outliers) >= 2:
                    good_idx = np.flatnonzero(~outliers)
                    peak_track = peak_track.copy()
                    peak_track[outliers] = np.interp(np.flatnonzero(outliers), good_idx, peak_track[good_idx])

        peak_track = np.nan_to_num(peak_track, nan=float(np.nanmedian(peak_track)))
        peak_track = peak_track - np.mean(peak_track)

        if peak_track.size < 2:
            return None

        sample_rate_hz = float(sample_rate_hz) if sample_rate_hz else 3000.0
        if sample_rate_hz <= 0:
            sample_rate_hz = 3000.0

        window = windows.hann(peak_track.size, sym=False) if peak_track.size > 1 else np.ones(peak_track.size, dtype=float)
        weighted_track = peak_track * window
        fft_values = np.fft.rfft(weighted_track)
        frequencies_hz = np.fft.rfftfreq(peak_track.size, d=1.0 / sample_rate_hz)
        magnitudes = np.abs(fft_values)

        if magnitudes.size == 0:
            return None

        magnitudes[0] = 0.0
        reference = float(np.max(magnitudes))
        if reference <= 0:
            magnitudes_db = np.full_like(magnitudes, -120.0, dtype=float)
        else:
            magnitudes_db = 20.0 * np.log10(np.maximum(magnitudes / reference, np.finfo(float).tiny))

        dominant_frequencies_hz: list[float] = []
        if frequencies_hz.size > 1:
            order = np.argsort(magnitudes[1:])[::-1] + 1
            dominant_frequencies_hz = frequencies_hz[order[:3]].astype(float).tolist()

        return {
            'peak_track': {
                'timestamps_s': (np.arange(peak_track.size, dtype=float) / sample_rate_hz).tolist(),
                'peak_positions': peak_track.tolist(),
                'peak_amplitudes': poi_amplitudes[:peak_track.size],
            },
            'fft': {
                'frequencies_hz': frequencies_hz.astype(float).tolist(),
                'magnitudes_db': magnitudes_db.astype(float).tolist(),
                'dominant_frequencies_hz': dominant_frequencies_hz,
            },
            'sample_rate_hz': sample_rate_hz,
            'valley': bool(valley),
        }

    except Exception as e:
        logger.error(f"Erro ao construir a FFT do traco temporal: {e}")
        return None