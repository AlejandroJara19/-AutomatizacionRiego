# --- INTERFAZ Y GRÁFICOS ---
import streamlit as st
import plotly.express as px
import plotly.io as pio
import folium
from streamlit_folium import st_folium

# --- MANEJO DE DATOS Y CÁLCULOS ---
import pandas as pd
import numpy as np
import math
import json
import datetime

# --- GEOPROCESAMIENTO (Sin GDAL pesado) ---
import rasterio
from rasterio.mask import mask
from rasterio.io import MemoryFile
import geopandas as gpd
from shapely.geometry import Point, mapping
from shapely.ops import transform

# --- UTILIDADES DEL SISTEMA ---
import os
import shutil
import tempfile
import zipfile
import io
import requests

# --- GENERACIÓN DE INFORMES (ADR) ---
from docx import Document
from docx.shared import Inches, Pt

# --- CACHÉ / RED ---
REQUEST_TIMEOUT = 30

@st.cache_data(show_spinner=False)
def fetch_nasa_data(lat, lon, fecha_inicio_str, fecha_fin_str):
    url_nasa = (
        f"https://power.larc.nasa.gov/api/temporal/daily/point"
        f"?parameters=PRECTOTCORR,T2M_MAX,T2M_MIN,EVPTRNS"
        f"&community=AG&longitude={lon}&latitude={lat}"
        f"&start={fecha_inicio_str}&end={fecha_fin_str}&format=JSON"
    )
    response = requests.get(url_nasa, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    params = data['properties']['parameter']
    df_nasa = pd.DataFrame({
        'Fecha': list(params['PRECTOTCORR'].keys()),
        'Precipitacion': list(params['PRECTOTCORR'].values()),
        'T_Max': list(params['T2M_MAX'].values()),
        'T_Min': list(params['T2M_MIN'].values()),
        'Evaporacion': list(params['EVPTRNS'].values())
    })
    df_nasa.replace(-999.0, np.nan, inplace=True)
    df_nasa.fillna(0, inplace=True)
    df_nasa['Fecha'] = pd.to_datetime(df_nasa['Fecha'], format='%Y%m%d')
    return df_nasa

def calcular_ret_vectorizado(df_nasa, lat_input):
    df = df_nasa.copy()
    df['DOY'] = df['Fecha'].dt.dayofyear
    lat_rad = math.radians(lat_input)

    t_max = df['T_Max'].to_numpy(dtype=float)
    t_min = df['T_Min'].to_numpy(dtype=float)
    doy = df['DOY'].to_numpy(dtype=float)
    t_mean = (t_max + t_min) / 2.0

    dr = 1.0 + 0.033 * np.cos(2.0 * np.pi * doy / 365.0)
    delta = 0.409 * np.sin((2.0 * np.pi * doy / 365.0) - 1.39)
    ws_arg = -np.tan(lat_rad) * np.tan(delta)
    ws_arg = np.clip(ws_arg, -1.0, 1.0)
    ws = np.arccos(ws_arg)

    ra = (24.0 * 60.0 / np.pi) * 0.0820 * dr * (
        ws * np.sin(lat_rad) * np.sin(delta) +
        np.cos(lat_rad) * np.cos(delta) * np.sin(ws)
    )
    ra_mm = ra * 0.408
    delta_t = np.maximum(t_max - t_min, 0.0)

    ret = np.where(
        t_max > t_min,
        0.0023 * ra_mm * (t_mean + 17.8) * np.sqrt(delta_t),
        0.0
    )
    df['RET'] = ret
    return df

def preparar_base_nasa(lat_input, lon_input, fecha_inicio, fecha_fin):
    f_inicio_nasa = fecha_inicio.strftime("%Y%m%d")
    f_fin_nasa = fecha_fin.strftime("%Y%m%d")
    df_nasa = fetch_nasa_data(lat_input, lon_input, f_inicio_nasa, f_fin_nasa)
    df_nasa = calcular_ret_vectorizado(df_nasa, lat_input)
    return df_nasa

def agregar_decadas(df_base_diario):
    df = df_base_diario.copy()
    df['Año'] = df['Fecha'].dt.year
    df['Mes'] = df['Fecha'].dt.month
    df['Día'] = df['Fecha'].dt.day
    df['Decada_Mes'] = np.select(
        [df['Día'] <= 10, df['Día'] <= 20],
        [1, 2],
        default=3
    )
    df['Decada_Año'] = (df['Mes'] - 1) * 3 + df['Decada_Mes']
    return df

# Inicialización de variables de estado (Session State)
# Coloca esto en la parte superior de tu app.py, fuera de cualquier pestaña
if 'zip_buffer' not in st.session_state:
    st.session_state.zip_buffer = None
if 'process_complete' not in st.session_state:
    st.session_state.process_complete = False

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="HidroApp ADR", page_icon="💧", layout="wide")
st.title("💧 HidroApp: Análisis y Descarga de Datos Climáticos")

def extraer_fecha_de_nombre(nombre_archivo):
    """
    Extrae fecha desde nombres con formatos:
    - YYYYMMDD
    - YYYY-MM-DD
    - YYYY_MM_DD
    """
    import re
    from datetime import datetime

    match = re.search(r'(\d{4})[-_]?(\d{2})[-_]?(\d{2})', nombre_archivo)
    if not match:
        return None

    year, month, day = match.groups()
    try:
        return datetime(int(year), int(month), int(day))
    except ValueError:
        return None

def procesar_zip_wapor(archivo_zip, lon, lat, nombre_columna):
    """
    Lee un archivo ZIP con rasters de WaPOR en memoria y extrae el valor para una coordenada.
    Retorna un DataFrame con 'Fecha' y la columna de la variable (ej. 'Precipitacion').
    """
    resultados = []
    archivos_tif = 0
    archivos_sin_fecha = 0
    archivos_con_error = 0

    with zipfile.ZipFile(archivo_zip) as z:
        for filename in z.namelist():
            if not filename.lower().endswith((".tif", ".tiff")):
                continue

            archivos_tif += 1

            fecha_extraida = extraer_fecha_de_nombre(os.path.basename(filename))
            if fecha_extraida is None:
                archivos_sin_fecha += 1
                continue

            fecha_obj = pd.to_datetime(fecha_extraida)

            try:
                with z.open(filename) as f:
                    with MemoryFile(f.read()) as memfile:
                        with memfile.open() as src:
                            muestra = list(src.sample([(lon, lat)]))
                            if not muestra or len(muestra[0]) == 0:
                                valor = np.nan
                            else:
                                valor = float(muestra[0][0])

                            # Limpieza de valores anómalos o "No Data" típicos de raster
                            if np.isnan(valor) or valor < -99 or valor > 10000:
                                valor = 0.0

                            resultados.append({'Fecha': fecha_obj, nombre_columna: valor})
            except Exception:
                archivos_con_error += 1
                continue

    # Retorno consistente para evitar KeyError al hacer merge por 'Fecha'
    if resultados:
        df_resultado = pd.DataFrame(resultados)
        df_resultado = df_resultado.sort_values('Fecha').drop_duplicates(subset=['Fecha']).reset_index(drop=True)
    else:
        df_resultado = pd.DataFrame(columns=['Fecha', nombre_columna])

    return df_resultado, {
        'archivos_tif': archivos_tif,
        'archivos_validos': len(resultados),
        'archivos_sin_fecha': archivos_sin_fecha,
        'archivos_con_error': archivos_con_error
    }

# --- CREACIÓN DE PESTAÑAS ---
tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Datos Agroclimáticos", 
    "💧 Balance Hídrico", 
    "📈 Volúmenes de Riego", 
    "⚙️ Generación WaPOR"
])
# =====================================================================
# --- PESTAÑA 1: ANÁLISIS DEL EXCEL / CSV --- (Sin cambios)
# =====================================================================
with tab1:
    st.markdown("Descarga la serie de la NASA o extrae información climática de repositorios Raster (WaPOR v3) para analizar la **Precipitación Confiable al 75%** y promedios decadales.")
    
    fuente_datos = st.radio("📡 Seleccione la fuente de datos:", ["NASA POWER (API Online)", "WaPOR v3 (Archivos Raster .ZIP)"], horizontal=True)
    
    col1, col2 = st.columns(2)
    with col1:
        lat_input = st.number_input("Latitud (Ej: 4.7160)", value=6.326512, format="%.6f", key="lat_t1")
    with col2:
        lon_input = st.number_input("Longitud (Ej: -74.2160)", value=-73.609413, format="%.6f", key="lon_t1")

    # Variable global de la pestaña que almacenará la serie diaria sin importar el origen
    df_base_diario = None 

    # ==========================================
    # RAMA 1: DESCARGA DESDE NASA POWER
    # ==========================================
    if fuente_datos == "NASA POWER (API Online)":
        col3, col4 = st.columns(2)
        with col3:
            fecha_inicio = st.date_input("Fecha Inicio", pd.to_datetime("2018-01-01"), key="fi_nasa")
        with col4:
            fecha_fin = st.date_input("Fecha Fin", pd.to_datetime("2025-12-31"), key="ff_nasa")

        if st.button("Obtener Datos NASA y Calcular Balance", type="primary"):
            with st.spinner('Consultando a la NASA y calculando variables hídricas... 🚀'):
                try:
                    df_base_diario = preparar_base_nasa(lat_input, lon_input, fecha_inicio, fecha_fin)
                    st.success("✅ ¡Datos de NASA descargados con éxito!")
                except Exception as e:
                    st.error(f"Error técnico con NASA: {e}")

    # ==========================================
    # RAMA 2: EXTRACCIÓN WAPOR v3 (.ZIP)
    # ==========================================
    elif fuente_datos == "WaPOR v3 (Archivos Raster .ZIP)":
        st.info("Sube los archivos .zip que contienen los TIFs de cada variable. El sistema cruzará la información con la coordenada ingresada.")
        
        col_w1, col_w2, col_w3 = st.columns(3)
        with col_w1:
            zip_precip = st.file_uploader("ZIP Precipitación", type="zip")
        with col_w2:
            zip_evap = st.file_uploader("ZIP Evaporación", type="zip")
        with col_w3:
            zip_ret = st.file_uploader("ZIP Evapotranspiración (RET)", type="zip")
            
        if st.button("Procesar Datos WaPOR y Calcular Balance", type="primary"):
            if zip_precip and zip_evap and zip_ret:
                with st.spinner('Extrayendo píxeles de los archivos Raster. Esto puede tardar unos segundos... 🛰️'):
                    try:
                        df_p, rep_p = procesar_zip_wapor(zip_precip, lon_input, lat_input, 'Precipitacion')
                        df_e, rep_e = procesar_zip_wapor(zip_evap, lon_input, lat_input, 'Evaporacion')
                        df_r, rep_r = procesar_zip_wapor(zip_ret, lon_input, lat_input, 'RET')

                        # Validaciones explícitas para evitar KeyError: 'Fecha'
                        faltantes = []
                        if df_p.empty:
                            faltantes.append(
                                f"Precipitación: 0 fechas válidas (TIF: {rep_p['archivos_tif']}, sin fecha: {rep_p['archivos_sin_fecha']}, error lectura: {rep_p['archivos_con_error']})"
                            )
                        if df_e.empty:
                            faltantes.append(
                                f"Evaporación: 0 fechas válidas (TIF: {rep_e['archivos_tif']}, sin fecha: {rep_e['archivos_sin_fecha']}, error lectura: {rep_e['archivos_con_error']})"
                            )
                        if df_r.empty:
                            faltantes.append(
                                f"RET: 0 fechas válidas (TIF: {rep_r['archivos_tif']}, sin fecha: {rep_r['archivos_sin_fecha']}, error lectura: {rep_r['archivos_con_error']})"
                            )

                        if faltantes:
                            st.error("No se pudieron extraer fechas/datos válidos de uno o más ZIP.")
                            for msg in faltantes:
                                st.warning(msg)
                        else:
                            # Unimos las 3 variables basadas en la fecha
                            df_clima = pd.merge(df_p, df_e, on='Fecha', how='inner')
                            df_clima = pd.merge(df_clima, df_r, on='Fecha', how='inner')

                            if df_clima.empty:
                                st.warning("No se encontraron fechas coincidentes entre los tres ZIP cargados.")
                            else:
                                df_base_diario = df_clima # Pasamos la estafeta al bloque común
                                st.success("✅ ¡Datos Raster procesados con éxito!")
                    except Exception as e:
                        st.error(f"Error procesando los archivos Raster: {e}")
            else:
                st.warning("Por favor, sube los 3 archivos ZIP para continuar.")


    # ==========================================
    # BLOQUE COMÚN: PROCESAMIENTO DECADAL Y GRÁFICAS
    # (Se ejecuta si df_base_diario fue llenado por NASA o por WaPOR)
    # ==========================================
    if df_base_diario is not None and not df_base_diario.empty:
        df_base_diario = agregar_decadas(df_base_diario)
        
        # 1. Sumar los valores por década para cada año individual
        df_decadal_anual = df_base_diario.groupby(['Año', 'Decada_Año'])[['Precipitacion', 'Evaporacion', 'RET']].sum().reset_index()
        
        # 2. Promediar Evaporación y RET a lo largo del periodo
        df_promedio_decadal = df_decadal_anual.groupby('Decada_Año')[['Evaporacion', 'RET']].mean().reset_index()
        
        # 3. Precipitación al 75% (Percentil 25)
        def calcular_prob_75(serie):
            if len(serie) == 0: return 0.0
            return np.percentile(serie, 25)

        precip_75_serie = df_decadal_anual.groupby('Decada_Año')['Precipitacion'].apply(calcular_prob_75).reset_index()
        precip_75_serie.rename(columns={'Precipitacion': 'Prec_75%'}, inplace=True)
        
        # Unir la precipitación calculada con los promedios
        df_promedio_decadal = pd.merge(df_promedio_decadal, precip_75_serie, on='Decada_Año')
        
        # --- EVIDENCIA DEL ANÁLISIS DE PROBABILIDAD (BLOM) ---
        st.subheader("🌧️ Análisis de Probabilidad (Precipitación ordenada)")
        try:
            df_prob_global = pd.DataFrame()
            for decada in range(1, 37):
                serie_decada = df_decadal_anual[df_decadal_anual['Decada_Año'] == decada]['Precipitacion'].values
                df_prob_global[f'D{decada}'] = np.sort(serie_decada)[::-1]
                
            N_anios = len(df_prob_global)
            m_arr = np.arange(1, N_anios + 1)
            prob_arr = ((m_arr - 0.375) / (N_anios + 0.25)) * 100
            
            df_prob_global.insert(0, 'Probabilidad Blom (%)', prob_arr)
            df_prob_global.insert(0, 'Orden (m)', m_arr)
            
            with st.expander("Ver matriz completa de ordenamiento y probabilidades"):
                st.dataframe(df_prob_global.round(2).style.format("{:.2f}", subset=['Probabilidad Blom (%)'] + [f'D{i}' for i in range(1, 37)]))
        except Exception as e:
            st.warning("No se pudo generar la tabla de visualización de probabilidad.")

        # --- GRÁFICA INTERACTIVA ---
        st.subheader("📈 Comportamiento Hídrico Decadal")
        df_melted = df_promedio_decadal.melt(
            id_vars=['Decada_Año'], value_vars=['Prec_75%', 'Evaporacion', 'RET'],
            var_name='Variable', value_name='Volumen (mm/década)'
        )
        
        import plotly.express as px
        fig = px.line(
            df_melted, x='Decada_Año', y='Volumen (mm/década)', color='Variable', 
            markers=True, color_discrete_map={'Prec_75%': '#1f77b4', 'Evaporacion': '#ff7f0e', 'RET': '#d62728'},
            labels={'Decada_Año': 'Década del Año (1 al 36)'}
        )
        fig.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1), hovermode="x unified")
        st.plotly_chart(fig)
        
        # --- GENERAR IMAGEN ESTÁTICA SEGURA (Matplotlib) ---
        try:
            import matplotlib.pyplot as plt
            import io
            fig_static, ax = plt.subplots(figsize=(8, 4))
            ax.plot(df_promedio_decadal['Decada_Año'], df_promedio_decadal['Prec_75%'], label='Prec_75%', color='#1f77b4', marker='o', markersize=4)
            ax.plot(df_promedio_decadal['Decada_Año'], df_promedio_decadal['Evaporacion'], label='Evaporacion', color='#ff7f0e', marker='o', markersize=4)
            ax.plot(df_promedio_decadal['Decada_Año'], df_promedio_decadal['RET'], label='RET', color='#d62728', marker='o', markersize=4)
            ax.set_title('Comportamiento Hídrico Decadal', fontsize=12)
            ax.set_xlabel('Década del Año (1 al 36)', fontsize=10)
            ax.set_ylabel('Volumen (mm/década)', fontsize=10)
            ax.legend()
            ax.grid(True, linestyle='--', alpha=0.7)
            
            img_buffer = io.BytesIO()
            fig_static.savefig(img_buffer, format='png', bbox_inches='tight', dpi=150)
            img_buffer.seek(0)
            st.session_state['imagen_clima_bytes'] = img_buffer.getvalue()
            plt.close(fig_static) 
        except Exception as e:
            st.session_state['imagen_clima_bytes'] = None

        # --- GUARDADO EN VARIABLES DE SESIÓN PARA LA PESTAÑA 2 ---
        st.session_state['latitud'] = lat_input
        st.session_state['longitud'] = lon_input
        st.session_state['df_promedio'] = df_promedio_decadal
        st.session_state['df_base_diario_tab1'] = df_base_diario.copy()
        
        # --- DESCARGA DE DATOS ---
        st.subheader("📥 Descarga de Resultados")
        col_down1, col_down2 = st.columns(2)
        
        with col_down1:
            with st.expander("Ver tabla de resultados decadales"):
                st.dataframe(df_promedio_decadal.round(2).style.format("{:.2f}"))
            csv_promedio = df_promedio_decadal.round(2).to_csv(index=False, sep=";")
            st.download_button("📥 Descargar Datos Decadales (CSV)", data=csv_promedio, file_name=f"Balance_Decadal_{lat_input}_{lon_input}.csv", mime="text/csv", key="btn_down_promedios")

        with col_down2:
            with st.expander("Ver datos diarios originales (NASA o WaPOR)"):
                st.dataframe(df_base_diario[['Fecha', 'Año', 'Mes', 'Día', 'Precipitacion', 'Evaporacion', 'RET']].head(100))
                st.caption("Mostrando los primeros 100 registros.")
            csv_diario = df_base_diario.round(2).to_csv(index=False, sep=";")
            st.download_button("📥 Descargar Serie Diaria (CSV)", data=csv_diario, file_name=f"Serie_Diaria_{lat_input}_{lon_input}.csv", mime="text/csv", key="btn_down_diario")
# =====================================================================
# --- PESTAÑA 2: DESCARGA AUTOMÁTICA NASA POWER --- (Sin cambios)
# =====================================================================

with tab2:
    st.markdown("### Balance Hídrico y Diseño Hidráulico")
    st.markdown("Determinación paso a paso de las necesidades netas, brutas y el dimensionamiento de caudales de diseño por sector.")
    
    # --- SECCIÓN 1: DATOS CLIMÁTICOS ---
    st.subheader("1. Ubicación y Periodo")
    fuente_clima_t2 = st.radio(
        "Fuente climática para Pestaña 2:",
        ["Usar datos procesados en Pestaña 1", "NASA POWER (API Online)"],
        horizontal=True,
        key="fuente_clima_t2"
    )

    col1, col2 = st.columns(2)
    with col1:
        lat_input = st.number_input("Latitud", value=6.326512, format="%.6f", key="lat_nasa_t2")
    with col2:
        lon_input = st.number_input("Longitud", value=-73.609413, format="%.6f", key="lon_nasa_t2")

    col3, col4 = st.columns(2)
    with col3:
        fecha_inicio = st.date_input("Fecha Inicio", pd.to_datetime("2018-01-01"), key="fi_nasa_t2")
    with col4:
        fecha_fin = st.date_input("Fecha Fin", pd.to_datetime("2025-12-31"), key="ff_nasa_t2")

# --- SECCIÓN 2: PARÁMETROS AGRONÓMICOS ---
    st.subheader("2. Parámetros Agronómicos y Fenología")
    ca1, ca2, ca3 = st.columns(3)
    with ca1:
        area_total = st.number_input("Área Total (Ha)", value=0.50, step=0.1, min_value=0.01, key="area_tot")
    with ca2:
        num_sectores = st.number_input("Número de Sectores", value=1, min_value=1, step=1, key="num_sect")
    with ca3:
        decada_inicio = st.number_input("Década de Inicio (1-36)", min_value=1, max_value=36, value=1, key="dec_ini")
        
    siembra_escalonada = st.checkbox("¿Aplicar siembra escalonada entre sectores?", value=True, key="check_esc")
    paso_escalonamiento = st.number_input("Décadas de espera entre siembra", min_value=0, value=1, key="paso_esc") if siembra_escalonada else 0

    st.markdown("**Fenología del Cultivo (Duración y Kc por etapa)**")
    ck1, ck2, ck3 = st.columns(3)
    with ck1:
        kc_ini = st.number_input("Kc Inicial", value=0.40, step=0.05, min_value=0.0, key="kc_i")
        dur_ini = st.number_input("Dur. Inicial", value=6, min_value=1, key="dur_i")
    with ck2:
        kc_mid = st.number_input("Kc Medio", value=1.10, step=0.05, min_value=0.0, key="kc_m")
        dur_mid = st.number_input("Dur. Media", value=6, min_value=1, key="dur_m")
    with ck3:
        kc_end = st.number_input("Kc Final", value=0.60, step=0.05, min_value=0.0, key="kc_f")
        dur_end = st.number_input("Dur. Final", value=5, min_value=1, key="dur_f")

    duracion_total = int(dur_ini + dur_mid + dur_end)
    sembrar_multiple = st.checkbox("Habilitar múltiples ciclos de producción", value=True, key="check_mult") if duracion_total < 36 else False
    descanso = st.number_input("Décadas de descanso", min_value=0, value=1, key="descanso") if sembrar_multiple else 0

    # --- SECCIÓN 3: CONFIGURACIÓN DEL SISTEMA DE RIEGO ---
    st.subheader("3. Configuración del Sistema de Riego")
    
    # Nuevo selector para elegir el tipo de riego
    tipo_riego = st.radio("Tipo de Riego", options=["Riego por goteo", "Riego por aspersión"], horizontal=True, key="tipo_riego")
    
    if tipo_riego == "Riego por goteo":
        cs1, cs2, cs3, cs4 = st.columns(4)
        with cs1: dist_emisores = st.number_input("Dist. Emisores (m)", value=0.20, step=0.05, min_value=0.01, key="dist_e")
        with cs2: dist_laterales = st.number_input("Dist. Laterales (m)", value=1.20, step=0.05, min_value=0.01, key="dist_l")
        with cs3: emisores_planta = st.number_input("Emisores / Planta", value=2, min_value=1, key="em_pl")
        with cs4: caudal_emisor_lh = st.number_input("Caudal Gotero (L/h)", value=1.00, step=0.1, min_value=0.01, key="q_got")
    else:
        # Inputs exclusivos para aspersión
        cs1, cs2 = st.columns(2)
        with cs1: num_aspersores_ha = st.number_input("Número de aspersores por ha", value=100, min_value=1, step=1, key="num_asp_ha")
        with cs2: caudal_emisor_lh = st.number_input("Caudal del Aspersor (L/h)", value=500.0, step=10.0, min_value=1.0, key="q_asp")

    # Inputs de eficiencia (siguen igual)
    ce1, ce2, ce3, ce4, ce5 = st.columns(5)
    with ce1: area_sombreada = st.number_input("% Sombreado", value=65.0, step=5.0, min_value=0.0, max_value=100.0, key="a_som")
    with ce2: pct_sustrato = st.number_input("% Sustrato", value=100.0, step=5.0, min_value=0.0, max_value=100.0, key="p_sus")
    with ce3: ef_cond = st.number_input("% Ef. Cond", value=98.0, step=1.0, min_value=0.0, max_value=100.0, key="ef_c")
    with ce4: ef_dist = st.number_input("% Ef. Dist", value=98.0, step=1.0, min_value=0.0, max_value=100.0, key="ef_d")
    with ce5: ef_rieg = st.number_input("% Ef. Riego", value=90.0, step=1.0, min_value=0.0, max_value=100.0, key="ef_r")

    if 'calcular_t2' not in st.session_state: st.session_state['calcular_t2'] = False
    if st.button("Calcular Diseño Paso a Paso", type="primary", key="btn_calc_t2_run"): st.session_state['calcular_t2'] = True

    if st.session_state['calcular_t2']:
        with st.spinner('Procesando datos hídricos, agronómicos e hidráulicos... 🚀'):
            try:
                import folium
                import streamlit.components.v1 as components

                # --- MAPA SATELITAL (DEPARTAMENTO Y MUNICIPIO) ---
                st.divider()
                st.subheader("🌍 Ubicación del Proyecto")
                m = folium.Map(location=[lat_input, lon_input], zoom_start=9)
                folium.TileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attr='Esri', name='Esri Satellite').add_to(m)
                folium.Marker([lat_input, lon_input], tooltip="Punto de Proyecto").add_to(m)
                
                try:
                    import time
                    headers = {'User-Agent': 'HidroApp_SIG_Project/1.0'}
                    
                    # Polígono del Departamento (Zoom 5)
                    url_dep = f"https://nominatim.openstreetmap.org/reverse?lat={lat_input}&lon={lon_input}&format=json&polygon_geojson=1&zoom=5"
                    res_dep = requests.get(url_dep, headers=headers)
                    if res_dep.status_code == 200:
                        data_dep = res_dep.json()
                        if 'geojson' in data_dep and data_dep['geojson']['type'] in ['Polygon', 'MultiPolygon']:
                            folium.GeoJson(
                                data_dep['geojson'], 
                                style_function=lambda x: {'fillColor': '#FFA500', 'color': '#FF8C00', 'weight': 2, 'fillOpacity': 0.1}, 
                                tooltip=f"Departamento: {data_dep.get('name', 'N/A')}"
                            ).add_to(m)
                    
                    time.sleep(1.5) # Pausa para no saturar la API
                    
                    # Polígono del Municipio (Zoom 10)
                    url_mun = f"https://nominatim.openstreetmap.org/reverse?lat={lat_input}&lon={lon_input}&format=json&polygon_geojson=1&zoom=10"
                    res_mun = requests.get(url_mun, headers=headers)
                    if res_mun.status_code == 200:
                        data_mun = res_mun.json()
                        if 'geojson' in data_mun and data_mun['geojson']['type'] in ['Polygon', 'MultiPolygon']:
                            folium.GeoJson(
                                data_mun['geojson'], 
                                style_function=lambda x: {'fillColor': '#00FFFF', 'color': '#008B8B', 'weight': 3, 'fillOpacity': 0.35}, 
                                tooltip=f"Municipio: {data_mun.get('name', 'N/A')}"
                            ).add_to(m)
                except Exception as e:
                    st.warning(f"Aviso SIG: No se pudieron cargar los límites políticos automáticos. ({e})")
                
                components.html(m._repr_html_(), height=400)

                # --- 1. FUENTE CLIMÁTICA (SESIÓN TAB1 O NASA) ---
                usar_sesion_tab1 = (
                    fuente_clima_t2 == "Usar datos procesados en Pestaña 1"
                    and 'df_base_diario_tab1' in st.session_state
                    and st.session_state['df_base_diario_tab1'] is not None
                    and not st.session_state['df_base_diario_tab1'].empty
                )

                if usar_sesion_tab1:
                    df_clima_t2 = st.session_state['df_base_diario_tab1'].copy()
                    if 'Decada_Año' not in df_clima_t2.columns:
                        df_clima_t2 = agregar_decadas(df_clima_t2)
                    st.info("Usando serie climática procesada en Pestaña 1 (NASA/WaPOR).")
                else:
                    if fuente_clima_t2 == "Usar datos procesados en Pestaña 1":
                        st.warning("No hay datos previos válidos en Pestaña 1. Se usará NASA POWER.")
                    df_clima_t2 = preparar_base_nasa(lat_input, lon_input, fecha_inicio, fecha_fin)
                    df_clima_t2 = agregar_decadas(df_clima_t2)

                df_dec_anual = df_clima_t2.groupby(['Año', 'Decada_Año'])[['Precipitacion', 'Evaporacion', 'RET']].sum().reset_index()
                df_prom = df_dec_anual.groupby('Decada_Año')[['Evaporacion', 'RET']].mean().reset_index()
                
                # --- 4. PRECIPITACIÓN AL 75% Y EFECTIVA ---
                def p75(s):
                    if len(s) == 0: 
                        return 0.0
                    return np.percentile(s, 25)

                # Integración decadal
                df_prom = pd.merge(df_prom, df_dec_anual.groupby('Decada_Año')['Precipitacion'].apply(p75).reset_index().rename(columns={'Precipitacion': 'Prec_75%'}), on='Decada_Año')
                P = df_prom['Prec_75%'].values
                
                # Cálculo de precipitación efectiva (Fórmula USDA)
                df_prom['Prec_Efectiva'] = np.where(P < (250/3), P*((125-0.6*P)/125), (125/3)+0.1*P)
                p_efec = df_prom['Prec_Efectiva'].values

                # --- 5. MATRICES KC Y ÁREA ---
                kc_m, area_m = np.zeros((num_sectores, 36)), np.zeros((num_sectores, 36))
                curva_kc = [kc_ini]*int(dur_ini) + [kc_mid]*int(dur_mid) + [kc_end]*int(dur_end)
                timeline = []
                while len(timeline) < 36:
                    timeline.extend(curva_kc)
                    if not sembrar_multiple: timeline.extend([0]*(36 - len(timeline))); break
                    else: timeline.extend([0]*int(descanso))
                timeline = timeline[:36] 
                
                for i in range(num_sectores):
                    idx = (int(decada_inicio)-1 + i*paso_escalonamiento)%36
                    for j in range(36):
                        d_act = (idx + j)%36
                        kc_m[i, d_act] = timeline[j]
                        if timeline[j] > 0: area_m[i, d_act] = area_total / num_sectores

                cols_d = [f"D{d}" for d in range(1, 37)]
                idx_s = [f"Sector {i+1}" for i in range(num_sectores)]
                df_kc, df_area = pd.DataFrame(kc_m, columns=cols_d, index=idx_s), pd.DataFrame(area_m, columns=cols_d, index=idx_s)
                
                df_ret_matrix = pd.DataFrame([df_prom['RET'].values], columns=cols_d, index=['Clima Base'])
                df_pefec_matrix = pd.DataFrame([p_efec], columns=cols_d, index=['Clima Base'])

                # --- 6. USO CONSUNTIVO Y CAUDALES ---
                uso_m, dem_n_m, dem_b_m = np.zeros((num_sectores,36)), np.zeros((num_sectores,36)), np.zeros((num_sectores,36))
                t_app_m, q_dem_m, q_dis_m = np.zeros((num_sectores,36)), np.zeros((num_sectores,36)), np.zeros((num_sectores,36))
                ef_g = (ef_cond/100)*(ef_dist/100)*(ef_rieg/100)
                
                # Arreglo de días de la década para cada mes (tiene en cuenta 8/9 para febrero dependiendo del año, 
                # en este arreglo estático se simplifican los días de cada década en el año).
                dias_d = np.array([10,10,11, 10,10,8, 10,10,11, 10,10,10, 10,10,11, 10,10,10, 10,10,11, 10,10,11, 10,10,10, 10,10,11, 10,10,10, 10,10,11])
                ret_v = df_prom['RET'].values
                
                # Pre-cálculo para aspersión: Intensidad de aplicación en mm/hr
                intensidad_app = 0
                if tipo_riego == "Riego por aspersión":
                    # Caudal emisor / Área por emisor
                    intensidad_app = caudal_emisor_lh / (10000 / num_aspersores_ha)

                for i in range(num_sectores):
                    for j in range(36):
                        if kc_m[i,j] > 0:
                            uso_m[i,j] = ret_v[j] * kc_m[i,j] * (pct_sustrato/100) * ((area_sombreada/100) + 0.15*(1 - (area_sombreada/100)))
                        
                        diff = uso_m[i,j] - p_efec[j]
                        dem_n_m[i,j] = diff if diff > 0 else 0
                        dem_b_m[i,j] = dem_n_m[i,j] / ef_g
                        
                        if dem_b_m[i,j] > 0:
                            # TIEMPO DE APLICACIÓN
                            if tipo_riego == "Riego por goteo":
                                t_app_m[i,j] = (dist_emisores*dist_laterales*(dem_b_m[i,j]/dias_d[j])*num_sectores) / (emisores_planta*caudal_emisor_lh)
                            else:
                                # Aspersión: = Demanda Bruta / (Intensidad * número de días década)
                                t_app_m[i,j] = dem_b_m[i,j] / (intensidad_app * dias_d[j])

                # Maximo tiempo de aplicación de toda la matriz como referencia (útil en goteo)
                t_max = t_app_m.max() if t_app_m.max() > 0 else 1
                
                # CAUDAL DEMANDADO Y DE DISEÑO
                for i in range(num_sectores):
                    for j in range(36):
                        if dem_b_m[i,j] > 0:
                            if tipo_riego == "Riego por goteo":
                                # Goteo: Se calcula el Caudal Específico (L/s por Hectárea)
                                q_dem_m[i,j] = ((dem_b_m[i,j]*10*1000)/(t_max*3600)) / dias_d[j]
                                # El Caudal de diseño del sector es el Específico multiplicado SOLO por el área del sector
                                q_dis_m[i,j] = q_dem_m[i,j] * area_m[i, j] 
                            else:
                                # Aspersión: Caudal instantáneo demandado por el sector (L/s)
                                q_dem_m[i,j] = (num_aspersores_ha * caudal_emisor_lh * area_m[i, j]) / 3600
                                
                                # Aspersión Diseño: Caudal continuo equivalente a 24h escalado a toda la finca
                                # (Caudal demandado * Tiempo aplicación / 24) * (Area total / Área sector)
                                q_dis_m[i,j] = (q_dem_m[i,j] * (t_app_m[i,j] / 24)) * (area_total / area_m[i, j])

                df_uso, df_dn, df_db = pd.DataFrame(uso_m, columns=cols_d, index=idx_s), pd.DataFrame(dem_n_m, columns=cols_d, index=idx_s), pd.DataFrame(dem_b_m, columns=cols_d, index=idx_s)
                df_t, df_qd, df_qdis = pd.DataFrame(t_app_m, columns=cols_d, index=idx_s), pd.DataFrame(q_dem_m, columns=cols_d, index=idx_s), pd.DataFrame(q_dis_m, columns=cols_d, index=idx_s)

                df_prom['Dem_Neta'] = dem_n_m.max(axis=0); df_prom['Dem_Bruta'] = dem_b_m.max(axis=0)
                df_prom['T_App'] = t_app_m.max(axis=0); df_prom['Q_Diseno'] = q_dis_m.sum(axis=0) 

                st.success("✅ Cálculos completados con éxito.")

                # --- FUNCIONES DE ESTILO VISUAL ---
                def color_kc(val): return 'background-color:#d4edda;color:black' if math.isclose(val,kc_ini) else 'background-color:#ffe8cc;color:black' if math.isclose(val,kc_mid) else 'background-color:#f8d7da;color:black' if math.isclose(val,kc_end) else ''
                
                # Nueva función para resaltar el valor máximo en toda la matriz
                def resaltar_maximo(df):
                    max_val = df.max().max()
                    # Crea un DataFrame vacío con la misma forma para almacenar los estilos
                    styles = pd.DataFrame('', index=df.index, columns=df.columns)
                    if max_val > 0:
                        # Aplica el estilo rojo solo a la celda que coincida con el máximo global
                        styles[df == max_val] = 'background-color: #ff4b4b; color: white; font-weight: bold;'
                    return styles

                # --- VISUALIZACIONES EN ACORDEONES ---
                st.divider()
                st.subheader("🌦️ Paso 1: Matrices Climáticas Base")
                with st.expander("Ver Matriz de Evapotranspiración de Referencia - RET (mm/década)"): st.dataframe(df_ret_matrix.style.format("{:.2f}"))
                with st.expander("Ver Matriz de Precipitación Efectiva (mm/década)"): st.dataframe(df_pefec_matrix.style.format("{:.2f}"))

                st.subheader("🌾 Paso 2: Matrices Agronómicas")
                with st.expander("Ver Matriz de Coeficiente de Cultivo (Kc)", expanded=True): st.dataframe(df_kc.style.map(color_kc).format("{:.2f}"))
                with st.expander("Ver Matriz de Área Sembrada por Sector (Ha)"): st.dataframe(df_area.style.format("{:.2f}"))
                with st.expander("Ver Matriz de Uso Consuntivo de la Planta (mm/década)"): st.dataframe(df_uso.style.format("{:.2f}"))

                st.subheader("💧 Paso 3: Matrices de Diseño Hidráulico")
                with st.expander("Ver Matriz de Demanda Neta (mm/década)"): st.dataframe(df_dn.style.format("{:.2f}"))
                with st.expander("Ver Matriz de Demanda Bruta (mm/década)"): st.dataframe(df_db.style.format("{:.2f}"))
                with st.expander("Ver Matriz de Tiempo de Aplicación (Horas/Día)"): st.dataframe(df_t.style.format("{:.3f}"))
                
                # Se aplica el estilo 'resaltar_maximo' a estas dos matrices específicas
                with st.expander("Ver Matriz de Caudal Demandado (L/s-ha)"): 
                    st.dataframe(df_qd.style.apply(resaltar_maximo, axis=None).format("{:.3f}"))
                with st.expander("Ver Matriz de Caudal de Diseño (L/s)"): 
                    st.dataframe(df_qdis.style.apply(resaltar_maximo, axis=None).format("{:.3f}"))

                st.divider()
                st.subheader("📊 Gráficas Generales del Sistema")
                import plotly.express as px
                df_agron = df_prom.melt(id_vars=['Decada_Año'], value_vars=['Prec_Efectiva', 'Dem_Neta', 'Dem_Bruta'], var_name='Variable', value_name='Volumen (mm/década)')
                fig_agron = px.line(df_agron, x='Decada_Año', y='Volumen (mm/década)', color='Variable', markers=True, color_discrete_map={'Prec_Efectiva': '#2ca02c', 'Dem_Neta': '#1f77b4', 'Dem_Bruta': '#d62728'}, title="Balance Hídrico (Máximos por década)")
                st.plotly_chart(fig_agron, use_container_width=True)

                st.info(f"⏱️ **Tiempo de aplicación máximo del sistema:** {t_max:.3f} horas/día.\n\n🌊 **Caudal Máximo del Sistema de Bombeo/Captación:** {df_prom['Q_Diseno'].max():.3f} L/s.")
                
                df_hidro = df_prom.melt(id_vars=['Decada_Año'], value_vars=['Q_Diseno', 'T_App'], var_name='Variable', value_name='Valor')
                fig_hidro = px.line(df_hidro, x='Decada_Año', y='Valor', color='Variable', markers=True, title="Comportamiento del Caudal Total y Tiempos de Riego", facet_row='Variable')
                fig_hidro.update_yaxes(matches=None)
                st.plotly_chart(fig_hidro, use_container_width=True)
                
                csv = df_prom.round(3).to_csv(index=False, sep=";")
                st.download_button("📥 Descargar Tabla General de Promedios y Totales (CSV)", data=csv, file_name=f"Diseno_Completo_{lat_input}_{lon_input}.csv", mime="text/csv", key="btn_down_final_t2")
                    
                st.session_state['df_chrono'] = df_dec_anual
                st.session_state['q_diseno_decadal'] = df_prom['Q_Diseno'].values
                st.session_state['t_max'] = t_max
                st.session_state['area_total_ha'] = area_total
                st.session_state['q_diseno'] = q_dis_m # Matriz de caudales

            except Exception as e:
                st.error("Error técnico al procesar el diseño hidráulico.")
                st.info(f"Detalle: {e}")

               
                
# =====================================================================
# --- PESTAÑA 3: FUNCIONAMIENTO RESERVORIO
# =====================================================================

with tab3:
    st.markdown("### Simulación Cronológica del Reservorio")
    st.markdown("Tránsito del embalse frente a la serie climática histórica para evaluar el riesgo de déficit. Incluye aportes por precipitación directa y cosecha de techos.")
    
    st.subheader("1. Dimensiones del Tanque Australiano")
    col1, col2, col3 = st.columns(3)
    with col1:
        radio_tanque = st.number_input("Radio del Tanque (m)", value=10.0, step=0.5, min_value=1.0)
    with col2:
        altura_tanque = st.number_input("Altura Útil Máxima (m)", value=1.50, step=0.1, min_value=0.5)
    with col3:
        caudal_concesion = st.number_input("Caudal Concesión Constante (L/s)", value=0.0, step=0.1, min_value=0.0, help="Entrada permanente de fuente externa.")

    st.subheader("2. Cosecha de Aguas Lluvias (Tejado/Ramada)")
    habilitar_cosecha = st.checkbox("¿Implementar cosecha de aguas lluvias mediante cubierta?", value=False, key="check_cosecha")
    
    if habilitar_cosecha:
        col4, col5, col6 = st.columns(3)
        with col4:
            largo_tejado = st.number_input("Largo Tejado (m)", value=10.0, step=1.0, min_value=0.0)
        with col5:
            ancho_tejado = st.number_input("Ancho Tejado (m)", value=10.0, step=1.0, min_value=0.0)
        with col6:
            coef_escorrentia = st.number_input("Coeficiente de Escorrentía", value=0.90, step=0.05, min_value=0.0, max_value=1.0)
    else:
        largo_tejado, ancho_tejado, coef_escorrentia = 0.0, 0.0, 0.0

    if st.button("Simular Tránsito del Reservorio", type="primary"):
        if 'df_chrono' not in st.session_state or 'q_diseno_decadal' not in st.session_state:
            st.warning("⚠️ Por favor, ejecuta primero el cálculo en la 'Pestaña 2' para generar las matrices de demanda.")
        else:
            with st.spinner("Simulando balance volumétrico y calculando optimización agronómica... 🌊"):
                df_chrono = st.session_state['df_chrono'].copy()
                q_diseno_decadal = st.session_state['q_diseno_decadal']
                t_max = st.session_state['t_max']
                area_cultivo_ha = st.session_state.get('area_total_ha', 0.5)
                
                # Geometría
                area_tanque = math.pi * (radio_tanque ** 2)
                v_max = area_tanque * altura_tanque
                area_tejado_efectiva = (largo_tejado * ancho_tejado) * coef_escorrentia
                area_tejado_fisica = largo_tejado * ancho_tejado
                
                dias_d = np.array([10,10,11, 10,10,8, 10,10,11, 10,10,10, 10,10,11, 10,10,10, 10,10,11, 10,10,11, 10,10,10, 10,10,11, 10,10,10, 10,10,11])
                
                resultados_simulacion = []
                v_actual = v_max 
                deficit_maximo_registrado = 0.0 
                
                for index, row in df_chrono.iterrows():
                    año, decada_año = int(row['Año']), int(row['Decada_Año'])
                    decada_idx = decada_año - 1 
                    dias, p_dec_mm, e_dec_mm = dias_d[decada_idx], row['Precipitacion'], row['Evaporacion']
                    
                    e_cp = (caudal_concesion * 86400 * dias) / 1000.0
                    e_ll = area_tanque * (p_dec_mm / 1000.0)
                    e_es = area_tejado_efectiva * (p_dec_mm / 1000.0)
                    
                    # Asumiendo que estás dentro del bucle de décadas y tienes acceso a 'decada_idx'
                    if tipo_riego == "Riego por goteo":
                        # Lógica original para goteo
                        s_d = (q_diseno_decadal[decada_idx] * t_max * 3600 * dias) / 1000.0
                    else:
                        # Lógica para aspersión: Caudal (L/s)
                        s_d = (q_diseno_decadal[decada_idx] * 86.4 * dias) 
                    s_e = area_tanque * (e_dec_mm / 1000.0)
                    s_i = s_e * 0.10
                    
                    v_temp = v_actual + e_cp + e_ll + e_es - s_d - s_e - s_i
                    derramado, deficit_decada = 0.0, 0.0
                    
                    if v_temp > v_max:
                        v_final, derramado, estado = v_max, v_temp - v_max, "Lleno (Derrama)"
                    elif v_temp < 0:
                        v_final, deficit_decada, estado = 0.0, abs(v_temp), "Déficit Crítico ⚠️"
                        v_actual_matematico = v_actual + e_cp + e_ll + e_es - s_d - s_e - s_i
                        if abs(v_actual_matematico) > deficit_maximo_registrado: deficit_maximo_registrado = abs(v_actual_matematico)
                    else:
                        v_final, estado = v_temp, "Operación Normal"
                        
                    altura_vaso = v_final / area_tanque if area_tanque > 0 else 0
                    
                    resultados_simulacion.append({
                        'Año': año, 'Decada': decada_año, 'Altura Vaso (m)': altura_vaso, 'Volumen Inicial (m3)': v_actual,
                        'Entrada Concesion (m3)': e_cp, 'Entrada Lluvia (m3)': e_ll, 'Entrada Escorrentia (m3)': e_es,
                        'Salida Riego (m3)': s_d, 'Salida Evaporación (m3)': s_e, 'Salida Infiltración (m3)': s_i,
                        'Volumen Final (m3)': v_final, 'Déficit Hídrico (m3)': deficit_decada, 'Volumen Derramado (m3)': derramado, 'Estado': estado
                    })
                    v_actual = v_final
                
                df_simulacion = pd.DataFrame(resultados_simulacion)
                st.success("✅ Simulación de tránsito del reservorio finalizada.")
                
                # --- PLANITO ESQUEMÁTICO A ESCALA REAL ---
                st.divider()
                st.subheader("🗺️ Esquema Espacial del Proyecto (Vista en Planta)")
                
                area_cultivo_m2 = area_cultivo_ha * 10000
                lado_cultivo = math.sqrt(area_cultivo_m2)
                
                import plotly.graph_objects as go
                fig_esq = go.Figure()

                fig_esq.add_shape(type="rect", x0=0, y0=0, x1=lado_cultivo, y1=lado_cultivo, line=dict(color="DarkOliveGreen", width=2), fillcolor="rgba(107,142,35,0.3)")
                fig_esq.add_trace(go.Scatter(x=[None], y=[None], mode='markers', marker=dict(size=15, color="rgba(107,142,35,0.5)", symbol='square'), name=f"Área Riego ({area_cultivo_m2:,.0f} m²)"))

                margen = max(lado_cultivo * 0.05, 2.0) 
                xc_tanque = lado_cultivo + margen + radio_tanque
                yc_tanque = radio_tanque
                fig_esq.add_shape(type="circle", x0=xc_tanque-radio_tanque, y0=yc_tanque-radio_tanque, x1=xc_tanque+radio_tanque, y1=yc_tanque+radio_tanque, line=dict(color="MidnightBlue", width=2), fillcolor="rgba(65,105,225,0.5)")
                fig_esq.add_trace(go.Scatter(x=[None], y=[None], mode='markers', marker=dict(size=15, color="rgba(65,105,225,0.5)", symbol='circle'), name=f"Reservorio ({area_tanque:,.0f} m²)"))

                if habilitar_cosecha and area_tejado_fisica > 0:
                    y_tej_base = yc_tanque + radio_tanque + margen
                    fig_esq.add_shape(type="rect", x0=lado_cultivo + margen, y0=y_tej_base, x1=lado_cultivo + margen + largo_tejado, y1=y_tej_base + ancho_tejado, line=dict(color="DimGray", width=2), fillcolor="rgba(169,169,169,0.6)")
                    fig_esq.add_trace(go.Scatter(x=[None], y=[None], mode='markers', marker=dict(size=15, color="rgba(169,169,169,0.6)", symbol='square'), name=f"Ramada ({area_tejado_fisica:,.0f} m²)"))

                fig_esq.update_layout(
                    xaxis=dict(scaleanchor="y", scaleratio=1, showgrid=False, zeroline=False, visible=False),
                    yaxis=dict(showgrid=False, zeroline=False, visible=False),
                    plot_bgcolor="white", margin=dict(l=0, r=0, t=30, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5)
                )
                st.plotly_chart(fig_esq, use_container_width=True)
                st.caption("🔍 *Nota: Este plano geométrico está renderizado a escala real (1:1). Ayuda a dimensionar la magnitud de las obras civiles frente a la extensión agrícola.*")

                # --- ANÁLISIS DE RESILIENCIA Y AUTO-DIMENSIONAMIENTO ---
                st.divider()
                st.subheader("🛠️ Diagnóstico de Resiliencia y Auto-Dimensionamiento")
                
                col_diag1, col_diag2 = st.columns(2)
                with col_diag1:
                    area_tejado_texto = f"{area_tejado_fisica:.2f} m²" if habilitar_cosecha else "No implementada"
                    st.info(f"**Diseño Actual Analizado:**\n* Radio Tanque: {radio_tanque} m\n* Volumen Tanque: {v_max:.2f} m³\n* Área Cultivo: {area_cultivo_ha:.2f} Ha\n* Área Ramada: {area_tejado_texto}")
                
                with col_diag2:
                    if deficit_maximo_registrado > 0:
                        st.error(f"🚨 **ALERTA DE QUIEBRE:** El sistema colapsó en época seca. Faltaron hasta **{deficit_maximo_registrado:.2f} m³** de agua en el peor momento.")
                        
                        # 1. Ajuste Estructural
                        volumen_ideal = v_max + deficit_maximo_registrado
                        radio_ideal = math.sqrt(volumen_ideal / (math.pi * altura_tanque))
                        
                        # 2. Ajuste Agronómico (Búsqueda Binaria del Área Óptima)
                        low, high = 0.001, area_cultivo_ha
                        area_optima = 0.0
                        
                        for _ in range(20): # 20 iteraciones son perfectas para precisión de 0.001 Ha
                            mid = (low + high) / 2
                            factor_area = mid / area_cultivo_ha
                            v_act_sim = v_max
                            fallo_sim = False
                            
                            for idx_s, row_s in df_chrono.iterrows():
                                d_idx = int(row_s['Decada_Año']) - 1
                                p_mm, e_mm = row_s['Precipitacion'], row_s['Evaporacion']
                                
                                e_cp_s = (caudal_concesion * 86400 * dias_d[d_idx]) / 1000.0
                                e_ll_s = area_tanque * (p_mm / 1000.0)
                                e_es_s = area_tejado_efectiva * (p_mm / 1000.0)
                                
                                s_d_s = (q_diseno_decadal[d_idx] * factor_area * t_max * 3600 * dias_d[d_idx]) / 1000.0
                                s_e_s = area_tanque * (e_mm / 1000.0)
                                s_i_s = s_e_s * 0.10
                                
                                v_temp_s = v_act_sim + e_cp_s + e_ll_s + e_es_s - s_d_s - s_e_s - s_i_s
                                
                                if v_temp_s < 0:
                                    fallo_sim = True
                                    break
                                v_act_sim = v_max if v_temp_s > v_max else v_temp_s
                                
                            if fallo_sim:
                                high = mid # El área probada sigue siendo muy grande
                            else:
                                low = mid  # El área soporta bien, intentemos con un poco más
                                area_optima = mid
                        
                        # Mostrar Soluciones
                        if radio_ideal > 20.0:
                            st.warning(f"🏗️ **Opción 1 (Estructural):** Subir el radio a **{radio_ideal:.2f} m** es inviable (>20m). Te recomendamos aumentar el área de la ramada.")
                        else:
                            st.info(f"🏗️ **Opción 1 (Estructural):** Incrementa el radio del tanque a **{radio_ideal:.2f} metros** para mantener las {area_cultivo_ha:.2f} Ha actuales de cultivo.")
                            
                        if area_optima > 0.01:
                            st.success(f"🌱 **Opción 2 (Agronómica):** Si no puedes agrandar el reservorio, debes reducir el área de cultivo a máximo **{area_optima:.3f} Ha** ({(area_optima*10000):.0f} m²) para garantizar agua todo el año.")
                        else:
                            st.error(f"🌱 **Opción 2 (Agronómica):** El reservorio propuesto es tan pequeño que no puede sostener ni 0.01 Ha. ¡Necesitas rediseñar urgentemente o buscar una concesión de agua!")
                    else:
                        st.success("🏆 **DISEÑO ÓPTIMO:** El reservorio propuesto es resiliente y no se vació durante toda la serie climática analizada.")

                # --- GRÁFICAS DEL COMPORTAMIENTO ---
                st.divider()
                st.subheader("📈 Evolución del Almacenamiento")
                df_simulacion['Periodo'] = df_simulacion['Año'].astype(str) + " - D" + df_simulacion['Decada'].astype(str).str.zfill(2)
                
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=df_simulacion['Periodo'], y=df_simulacion['Volumen Final (m3)'], mode='lines', fill='tozeroy', name='Volumen Almacenado', line=dict(color='#1f77b4')))
                fig.add_trace(go.Scatter(x=df_simulacion['Periodo'], y=[v_max]*len(df_simulacion), mode='lines', name='Capacidad Máxima', line=dict(color='red', dash='dash')))
                
                fig.update_layout(title="Tránsito del Embalse a lo Largo de los Años", xaxis_title="Periodo (Año - Década)", yaxis_title="Volumen (m³)", hovermode="x unified")
                st.plotly_chart(fig, use_container_width=True)

                # --- MATRIZ DE RESULTADOS ---
                st.divider()
                st.subheader("📋 Matriz de Tránsito del Reservorio")
                
                def color_estado(val):
                    if "Déficit" in val: return 'background-color: #ff4b4b; color: white;'
                    elif "Derrama" in val: return 'background-color: #1f77b4; color: white;'
                    return 'background-color: #d4edda; color: black;'
                
                with st.expander("Ver Matriz Completa de Simulación", expanded=True):
                    st.dataframe(df_simulacion.style.map(color_estado, subset=['Estado']).format({
                        'Altura Vaso (m)': '{:.2f}', 'Volumen Inicial (m3)': '{:.2f}', 'Entrada Concesion (m3)': '{:.2f}', 
                        'Entrada Lluvia (m3)': '{:.2f}', 'Entrada Escorrentia (m3)': '{:.2f}', 'Salida Riego (m3)': '{:.2f}', 
                        'Salida Evaporación (m3)': '{:.2f}', 'Salida Infiltración (m3)': '{:.2f}', 'Volumen Final (m3)': '{:.2f}', 
                        'Déficit Hídrico (m3)': '{:.2f}', 'Volumen Derramado (m3)': '{:.2f}'
                    }))

                csv_sim = df_simulacion.round(3).to_csv(index=False, sep=";")
                st.download_button("📥 Descargar Matriz de Simulación (CSV)", data=csv_sim, file_name="Simulacion_Reservorio.csv", mime="text/csv", key="btn_down_sim")



# --- PESTAÑA 6: GENERACIÓN DE MEMORIA DE CÁLCULO ---
with tab4:
    st.subheader("📄 Generación de Memoria de Cálculo")
    st.markdown("Consolida todos los parámetros climáticos, agronómicos e hidráulicos calculados en las pestañas anteriores en un documento formal de Word.")

    # Definimos la función generadora de Word aquí mismo para mantener el orden
    def generar_memoria_calculo():
        # 1. Crear documento en blanco
        doc = Document()
        
        # 2. Título y Encabezado
        doc.add_heading('Memoria de Cálculo: Sistema de Riego', 0)
        doc.add_paragraph('Generado automáticamente por HidroApp ADR')
        
        # 3. Sección de Parámetros de Ubicación y Clima
        doc.add_heading('1. Parámetros de Ubicación y Climatología', level=1)
        lat = st.session_state.get('latitud', 'N/A') # Revisa si tu variable se llama lat_nasa_t1
        lon = st.session_state.get('longitud', 'N/A')
        doc.add_paragraph(f'Coordenadas del proyecto: Latitud {lat}, Longitud {lon}')
        doc.add_paragraph('Los datos climáticos fueron extraídos de la base de datos de NASA POWER. La precipitación confiable se calculó utilizando el Percentil 25 (probabilidad de excedencia del 75%).')

        # 4. Insertar Gráficos
        if st.session_state.get('imagen_clima_bytes') is not None:
            # Ya no usamos pio.to_image aquí, simplemente llamamos la foto guardada
            img_buffer = io.BytesIO(st.session_state['imagen_clima_bytes'])
            doc.add_picture(img_buffer, width=Inches(6.0))
            doc.add_paragraph('Figura 1. Comportamiento hídrico decadal histórico.')
        else:
            doc.add_paragraph('(Aviso: La gráfica climática no pudo ser procesada como imagen estática para este documento).')

        # 5. Sección de Diseño Agronómico e Hidráulico
        doc.add_heading('2. Diseño Agronómico y Requerimientos de Caudal', level=1)
        tipo = st.session_state.get('tipo_riego', 'No definido')
        area = st.session_state.get('area_total_ha', 'N/A')
        doc.add_paragraph(f'Tipo de sistema seleccionado: {tipo}')
        doc.add_paragraph(f'Área total del proyecto: {area} hectáreas.')
        
        # --- AÑADIMOS NUEVAS SECCIONES BASADAS EN LOS CÁLCULOS ---
        doc.add_heading('3. Resultados Hidráulicos', level=1)
        
        # Verificamos si la matriz de diseño se guardó en la memoria
        if 'q_diseno' in st.session_state:
            q_matriz = st.session_state['q_diseno']
            
            # Sumar todos los caudales de los sectores (asumiendo que están en la matriz)
            caudal_maximo_total = q_matriz.sum(axis=0).max() # Caudal pico en la peor década
            
            doc.add_paragraph('El sistema fue evaluado para 36 décadas, proyectando el requerimiento a 24 horas continuas para determinar la capacidad de la fuente.')
            
            # Escribir el caudal máximo en negrita
            p = doc.add_paragraph()
            p.add_run('Caudal de diseño máximo requerido (Estimado): ').bold = True
            p.add_run(f'{caudal_maximo_total:.2f} Litros/segundo.')
        else:
            doc.add_paragraph('No se encontraron resultados de caudal en la memoria. Asegúrese de calcular el balance hídrico.')
            
        # 6. Guardar en un buffer de memoria
        buffer_salida = io.BytesIO()
        doc.save(buffer_salida)
        buffer_salida.seek(0)
        
        return buffer_salida

    # --- FIN DE LA FUNCIÓN ---
    # El código a continuación vuelve a alinearse a la izquierda (fuera del def, dentro del with tab6)

    # Botón para generar y descargar
    if st.button("Generar Informe Técnico en Word", type="primary"):
        with st.spinner("Redactando memoria de cálculo y procesando gráficas... ✍️"):
            try:
                # Llamamos a la función
                archivo_docx = generar_memoria_calculo()
                
                # Mostramos el botón de descarga real
                st.success("✅ Informe generado exitosamente.")
                st.download_button(
                    label="📥 Descargar Memoria_Calculo.docx",
                    data=archivo_docx,
                    file_name="Memoria_Calculo_HidroApp.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
            except Exception as e:
                st.error(f"Hubo un error al generar el documento: {e}")
                st.info("Asegúrate de haber calculado las pestañas anteriores primero.")