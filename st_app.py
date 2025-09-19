import os
import warnings
warnings.filterwarnings("ignore")

# --- Core ---
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# --- Stats/TS ---
from statsmodels.tsa.seasonal import seasonal_decompose, STL

# --- Data ---
from pymongo import MongoClient
from dotenv import load_dotenv
import pydeck as pdk

# =============================
# 1) Page & Theme
# =============================
st.set_page_config(page_title="Traffic Analytics — By Category", layout="wide")
st.title("🚦 Traffic Analytics — 分類版 (Time / Spatial / Trends / Correlation)")
st.caption("聚焦所選月份，視覺放大可讀。支援 STL 季節/趨勢分解與外部因素相關性分析。")

# =============================
# 2) DB Connection (cached)
# =============================
@st.cache_resource
def get_mongo_client():
    # No need for load_dotenv() anymore for deployment
    uri = st.secrets["MONGO_URI"] # <-- USE THIS INSTEAD
    if not uri:
        st.error("MONGO_URI not found in Streamlit Secrets!")
        return None
    try: 
        client = MongoClient(uri)
        client.admin.command('ping')
        return client
    except Exception as e:
        st.error(f"Failed to connect to MongoDB: {e}")
        return None
# =============================
# 3) Data Load & Prep (cached)
# =============================
def _standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        'traffic_volume (vehicles/hour)': 'traffic_volume',
        'average_speed (km/h)': 'average_speed',
        'Date_Time': 'datetime',
        'Date_time': 'datetime',
        'date_time': 'datetime'
    }
    df = df.rename(columns=rename_map)
    num_cols = ['traffic_volume', 'average_speed', 'incidents', 'latitude', 'longitude']
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    if 'datetime' in df.columns:
        df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce')
    if 'incidents' not in df.columns:
        df['incidents'] = 0
    if {'latitude', 'longitude'}.issubset(df.columns):
        df = df.dropna(subset=['latitude', 'longitude'])
    return df

@st.cache_data(ttl=3600)
def load_data(_client, database_name: str, month: str) -> pd.DataFrame:
    if _client is None:
        return pd.DataFrame()
    try:
        db = _client.get_database(database_name)
        if month not in db.list_collection_names():
            return pd.DataFrame()
        collection = db.get_collection(month)
        data = list(collection.find({}))
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        for col in ['_id', 'traffic_id', 'region_id', 'city']:
            if col in df.columns:
                df.drop(columns=col, inplace=True)
        df = _standardize_columns(df)
        if 'datetime' in df.columns:
            df['hour'] = df['datetime'].dt.hour
            df['dow'] = df['datetime'].dt.dayofweek
            df['is_weekend'] = df['dow'].isin([5, 6]).astype(int)
            df['month'] = df['datetime'].dt.month
            df['year'] = df['datetime'].dt.year
        return df.drop_duplicates()
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        return pd.DataFrame()

@st.cache_data
def to_csv_bytes(df: pd.DataFrame):
    return df.to_csv(index=False).encode('utf-8')

# Utils
def winsorize(frame: pd.DataFrame, cols, q=(0.01, 0.99)):
    df = frame.copy()
    for c in cols:
        if c in df.columns:
            lo, hi = df[c].quantile(q)
            df[c] = df[c].clip(lo, hi)
    return df

# =============================
# 4) Sidebar Filters
# =============================
client = get_mongo_client()
if not client:
    st.stop()

st.sidebar.header("🔧 Filters")
db_list = ["historical_newyork", "historical_la", "historical_georgia", "historical_sydney"]
selected_db = st.sidebar.selectbox("Dataset", db_list, index=1)

# Month selection with names
month_names = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]
selected_month = st.sidebar.selectbox("Month", month_names, index=0)


# Data guards
st.sidebar.markdown("---")
st.sidebar.header("🧹 Data Quality")
max_speed = st.sidebar.number_input("Max speed (km/h)", 20, 200, 160, 5)
max_volume = st.sidebar.number_input("Max vehicles/hour", 200, 20000, 10000, 100)
robust_view = st.sidebar.checkbox("Robust view (winsorize 1–99%)", value=True)

# Load
_df = load_data(client, selected_db, selected_month)
if _df.empty:
    st.warning(f"No data found in **{selected_db} / {selected_month}**")
    st.stop()

# Apply guards
if 'average_speed' in _df.columns:
    _df.loc[_df['average_speed'] > max_speed, 'average_speed'] = np.nan
if 'traffic_volume' in _df.columns:
    _df.loc[_df['traffic_volume'] > max_volume, 'traffic_volume'] = np.nan
if robust_view:
    _df = winsorize(_df, ['traffic_volume', 'average_speed'])

# Region filter
regions = ["(All)"] + sorted(_df.get('region_name', pd.Series(dtype=str)).dropna().unique().tolist())
selected_regions = st.sidebar.multiselect("Regions", regions, default=["(All)"])
if "(All)" not in selected_regions:
    _df = _df[_df['region_name'].isin(selected_regions)]

# =============================
# KPIs（含小問號說明）
# =============================
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Rows", f"{len(_df):,}", help="符合目前篩選（Dataset / Month / Regions / Data Quality）後的資料筆數。")
k2.metric("Avg speed", f"{_df['average_speed'].mean():.1f} km/h", help="平均速度＝目前篩選資料的平均。")
k3.metric("Total volume", f"{int(_df['traffic_volume'].sum()):,}", help="traffic_volume 直加總。")
k4.metric("Incidents", f"{int(_df['incidents'].sum()):,}", help="incidents 欄位加總。")
if 'datetime' in _df.columns and _df['datetime'].notna().any():
    coverage_days = int((_df['datetime'].max() - _df['datetime'].min()).days) + 1
    k5.metric("Coverage (days)", f"{coverage_days}", help="含首尾天數。")
else:
    k5.metric("Coverage (days)", "—", help="資料內沒有可用的時間戳。")

st.markdown("---")

# =============================
# 5) Tabs by Category
# =============================
TAB_TIME, TAB_SPATIAL, TAB_TREND, TAB_CORR = st.tabs([
    "⏱️ 時間分析 (Time)", "📍 空間分析 (Spatial)", "📈 趨勢分析 (Trends)", "🔗 相關性分析 (Correlation)"
])

# ---------- 時間分析 ----------
with TAB_TIME:
    st.subheader("只聚焦所選月份（可切換年份/粒度/極值標記）")
    if 'datetime' not in _df.columns:
        st.info("No datetime available.")
    else:
        month_num = month_names.index(selected_month) + 1
        df_m = _df[_df['datetime'].dt.month == month_num].copy()
        years = sorted(df_m['datetime'].dt.year.dropna().unique())
        if not years:
            st.info("No rows for selected month.")
        else:
            c1, c2, c3 = st.columns([1, 1, 1])
            focus_year = c1.selectbox("Year", years, index=len(years) - 1)
            gran = c2.radio("Granularity", ["Hourly", "Daily"], horizontal=True, index=0)
            marks = c3.slider("Mark top highs/lows", 0, 10, 3)

            df_f = df_m[df_m['datetime'].dt.year == focus_year].copy()
            if df_f.empty:
                st.info(f"No data for {focus_year}-{month_num:02d}.")
            else:
                rule = 'H' if gran == "Hourly" else 'D'
                ts = (
                    df_f.set_index('datetime').sort_index()
                    .resample(rule).agg({'traffic_volume': 'mean', 'average_speed': 'mean'})
                )

                # dynamic smoother window
                def _win(n): return int(np.clip(max(3, n // 20), 3, 24))
                w = _win(len(ts))
                ts['vol_ma'] = ts['traffic_volume'].rolling(w, min_periods=1).mean()
                ts['spd_ma'] = ts['average_speed'].rolling(w, min_periods=1).mean()

                # helper to mark extremes
                def _mark(s, n):
                    if n <= 0 or s.dropna().empty:
                        return pd.Series(dtype=float), pd.Series(dtype=float)
                    return s.nlargest(n), s.nsmallest(n)

                vol_hi, vol_lo = _mark(ts['traffic_volume'], marks)
                spd_hi, spd_lo = _mark(ts['average_speed'], marks)

                t1, t2 = st.columns(2)
                start, end = ts.index.min(), ts.index.max()

                with t1:
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=ts.index, y=ts['traffic_volume'], name='Volume', line=dict(width=1.5)))
                    fig.add_trace(go.Scatter(x=ts.index, y=ts['vol_ma'], name=f'{w}-pt MA', line=dict(width=3)))
                    if marks > 0 and len(vol_hi):
                        fig.add_trace(go.Scatter(x=vol_hi.index, y=vol_hi.values, mode='markers+text', name='Highs',
                                                 text=[f"{v:.0f}" for v in vol_hi.values], textposition='top center',
                                                 marker=dict(size=9, symbol='triangle-up')))
                    if marks > 0 and len(vol_lo):
                        fig.add_trace(go.Scatter(x=vol_lo.index, y=vol_lo.values, mode='markers+text', name='Lows',
                                                 text=[f"{v:.0f}" for v in vol_lo.values], textposition='bottom center',
                                                 marker=dict(size=9, symbol='triangle-down')))
                    fig.update_layout(title=f"Traffic Volume — {focus_year}-{month_num:02d} ({gran})", height=460,
                                      xaxis=dict(rangeslider=dict(visible=True), range=[start, end]), yaxis_title='veh/h',
                                      legend=dict(orientation='h', y=1.05))
                    st.plotly_chart(fig, use_container_width=True)

                with t2:
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=ts.index, y=ts['average_speed'], name='Avg Speed', line=dict(width=1.5)))
                    fig.add_trace(go.Scatter(x=ts.index, y=ts['spd_ma'], name=f'{w}-pt MA', line=dict(width=3)))
                    if marks > 0 and len(spd_hi):
                        fig.add_trace(go.Scatter(x=spd_hi.index, y=spd_hi.values, mode='markers+text', name='Highs',
                                                 text=[f"{v:.1f}" for v in spd_hi.values], textposition='top center',
                                                 marker=dict(size=9, symbol='triangle-up')))
                    if marks > 0 and len(spd_lo):
                        fig.add_trace(go.Scatter(x=spd_lo.index, y=spd_lo.values, mode='markers+text', name='Lows',
                                                 text=[f"{v:.1f}" for v in spd_lo.values], textposition='bottom center',
                                                 marker=dict(size=9, symbol='triangle-down')))
                    fig.update_layout(title=f"Average Speed — {focus_year}-{month_num:02d} ({gran})", height=460,
                                      xaxis=dict(rangeslider=dict(visible=True), range=[start, end]), yaxis_title='km/h',
                                      legend=dict(orientation='h', y=1.05))
                    st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    st.subheader("（選配）季節/趨勢分解 — 單一軸控制（三排同步）")
    if 'datetime' in _df.columns:
        month_num = month_names.index(selected_month) + 1
        df_m = _df[_df['datetime'].dt.month == month_num].copy()
        years = sorted(df_m['datetime'].dt.year.dropna().unique().tolist())
        if years:
            col_y, col_alg = st.columns([1, 2])
            decomp_year = col_y.selectbox("Year", years, index=len(years) - 1, key="dec_year")
            algo = col_alg.selectbox(
                "Method",
                ["STL (robust)", "Seasonal Decompose (additive)"], index=0,
                help=("STL (robust)：以 LOESS 平滑分離趨勢/季節，對離群值較不敏感；"
                      "Seasonal Decompose (additive)：季節形狀固定、計算較快。")
            )

            df_my = df_m[df_m['datetime'].dt.year == decomp_year].copy()
            ts_hourly = df_my.set_index('datetime').sort_index()['traffic_volume'].resample('H').mean().ffill()
            n = len(ts_hourly)
            if n >= 48:
                period = 24
                if algo.startswith("STL"):
                    def _odd(k):
                        k = int(max(3, k)); return k if k % 2 == 1 else k + 1
                    seasonal_w = _odd(min(max(11, period), max(7, n // 8)))
                    trend_w    = _odd(min(max(35, period * 5), max(7, n // 2)))
                    st.caption(f"STL params → period={period}, seasonal={seasonal_w}, trend={trend_w}, robust=True")
                    stl = STL(ts_hourly, period=period, seasonal=seasonal_w, trend=trend_w, robust=True)
                    res = stl.fit()
                    obs, trend, seas, resid = ts_hourly, res.trend, res.seasonal, res.resid
                else:
                    dec = seasonal_decompose(ts_hourly, model='additive', period=period)
                    obs, trend, seas, resid = dec.observed, dec.trend, dec.seasonal, dec.resid

                # ---- 控制列 ----
                c_roll1, c_roll2, c_roll3 = st.columns([1, 1, 2])
                max_win = int(min(168, n))  # 最多 7 天
                roll_h = c_roll1.slider("Residual rolling window (hours)", 3, max_win, min(24, max_win), 1)
                roll_stat = c_roll2.selectbox("Aggregate", ["mean", "sum", "std", "abs_sum"], index=0)
                bottom_mode = c_roll3.radio("Bottom panel shows", ["Seasonal", "Residual", "Seasonal & Residual"],
                                            index=2, horizontal=True)

                resid_roll = (resid.abs().rolling(roll_h, min_periods=1).sum()
                              if roll_stat == "abs_sum"
                              else getattr(resid.rolling(roll_h, min_periods=1), roll_stat)())

                # ---- 三排：上/中/下（中排只要 bar）----
                fig = make_subplots(
                    rows=3, cols=1, shared_xaxes=True,
                    specs=[[{"type": "xy"}], [{"type": "bar"}], [{"type": "xy"}]],
                    vertical_spacing=0.10,
                    row_heights=[0.52, 0.18, 0.30],
                    subplot_titles=(
                        f"Observed & Trend — {decomp_year}-{month_num:02d}",
                        f"Residual (rolling {roll_stat}, {roll_h}h) — Navigator",
                        "Seasonal / Residual (choose above)"
                    )
                )

                # Row 1：Observed + Trend（線）
                fig.add_trace(go.Scatter(x=obs.index, y=obs.values, name="Observed", line=dict(width=1.5)), row=1, col=1)
                fig.add_trace(go.Scatter(x=trend.index, y=trend.values, name="Trend", line=dict(width=3)), row=1, col=1)

                # Row 2：只放 rolling bar（不加任何其他元素/線/shape/legend）
                fig.add_trace(
                     go.Scatter(
                        x=resid_roll.index, 
                        y=resid_roll.values, 
                        fill='tozeroy',  mode='none',     
                        showlegend=False,fillcolor='rgba(255, 82, 82, 0.5)' ),row=2, col=1
                )

                # Row 3：Seasonal / Residual（線）
                if bottom_mode in ["Seasonal", "Seasonal & Residual"]:
                    fig.add_trace(go.Scatter(x=seas.index, y=seas.values, name="Seasonal", line=dict(width=1.5)), row=3, col=1)
                if bottom_mode in ["Residual", "Seasonal & Residual"]:
                    fig.add_trace(go.Scatter(x=resid.index, y=resid.values, name="Residual", line=dict(width=1)), row=3, col=1)

                # 同步 x；rangeslider 只在中排
                # fig.update_xaxes(matches='x')
                # fig.update_xaxes(rangeslider=dict(visible=True), row=3, col=1)


                fig.update_layout(
                    height=740, margin=dict(t=110, b=60, l=70, r=30),
                    yaxis_title="veh/h", bargap=0.02, uirevision="stl_decomp_sync"
                )

                # 調整標題外觀
                if hasattr(fig.layout, "annotations") and fig.layout.annotations:
                    for ann in fig.layout.annotations:
                        ann.y += 0.05
                        ann.font.size = 14
                        ann.bgcolor = "rgba(30,30,30,0.6)"
                        ann.borderpad = 4

                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Not enough hourly points in this month/year (need ≥ 48).")
        else:
            st.info("No rows for the selected month.")

# ---------- 空間分析 (Spatial) ----------
with TAB_SPATIAL:
    st.subheader("地圖熱點 + 區域彙整")
    
    # Check for necessary columns
    if {'latitude', 'longitude', 'traffic_volume'}.issubset(_df.columns):
        c1, c2 = st.columns([2, 1])
        with c1:
            st.pydeck_chart(pdk.Deck(
                map_style='mapbox://styles/mapbox/dark-v9',
                initial_view_state=pdk.ViewState(
                    latitude=_df['latitude'].mean(),
                    longitude=_df['longitude'].mean(),
                    zoom=9,
                    pitch=50,
                ),
                layers=[
                    pdk.Layer(
                        'HexagonLayer',
                        data=_df[['longitude', 'latitude', 'traffic_volume']].dropna(),
                        get_position='[longitude, latitude]',
                        radius=200,
                        elevation_scale=4,
                        elevation_range=[0, 1000],
                        pickable=True,
                        extruded=True,
                    ),
                ],
                tooltip={"text": "Volume in this area: {elevationValue}"}
            ))
        with c2:
            st.info("""
            **地圖說明**
            - **熱力圖**: 顯示交通流量的地理集中度。
            - **高度**: 柱體越高，代表該區域的總流量越大。
            - **互動**: 可縮放、平移、旋轉地圖以檢視細節。
            """)
            if 'region_name' in _df.columns:
                st.metric("涵蓋區域數", _df['region_name'].nunique())
            st.metric("地理座標點", f"{len(_df[['latitude', 'longitude']].dropna()):,}")

    else:
        st.info("缺少 'latitude', 'longitude' 或 'traffic_volume' 欄位，無法繪製地圖。")

    st.markdown("---")

    if 'region_name' in _df.columns:
        st.subheader("各區域流量與速度彙總")
        agg = _df.groupby('region_name').agg(
            total_volume=('traffic_volume', 'sum'),
            avg_speed=('average_speed', 'mean'),
            incident_count=('incidents', 'sum'),
            record_count=('traffic_volume', 'count')
        ).reset_index().sort_values('total_volume', ascending=False)
        
        c1, c2 = st.columns(2)
        with c1:
            fig = px.bar(agg, x='total_volume', y='region_name', orientation='h', title='Total Volume by Region')
            fig.update_layout(height=450, yaxis={'categoryorder':'total ascending'})
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            fig = px.pie(agg, names='region_name', values='total_volume', title='Volume Share by Region', hole=0.35)
            fig.update_layout(height=450)
            st.plotly_chart(fig, use_container_width=True)
        
        st.dataframe(agg)
        st.download_button("Download regional summary CSV", data=to_csv_bytes(agg),
                           file_name=f"{selected_db}_{selected_month}_regional_summary.csv")
    else:
        st.info("No 'region_name' column to aggregate by.")

# ---------- 趨勢分析 (Trends) - Redesigned ----------
with TAB_TREND:
    st.subheader("📈 每日趨勢與週間模式分析")

    # ----- 互動式選項 -----
    c1, c2 = st.columns([1, 2])
    # 讓使用者選擇要分析的指標
    selected_metric = c1.radio(
        "選擇分析指標 (Select Metric)",
        ['traffic_volume', 'average_speed'],
        format_func=lambda x: '車流量 (Volume)' if x == 'traffic_volume' else '平均車速 (Speed)',
        horizontal=True
    )
    
    # 根據選擇的指標設定圖表標題
    metric_label = "中位數車流量 (Median Volume)" if selected_metric == 'traffic_volume' else "中位數車速 (Median Speed)"
    yaxis_title = "車輛/小時 (veh/h)" if selected_metric == 'traffic_volume' else "公里/小時 (km/h)"

    st.markdown("---")

    # ----- 圖表一：每日流量/速度趨勢圖 (週間 vs. 週末) -----
    st.subheader(f"平日 vs. 週末的 {metric_label} 變化")
    
    if {'dow', 'hour', selected_metric}.issubset(_df.columns):
        # 1. 建立一個新欄位來區分工作日與週末
        df_trend = _df.copy()
        df_trend['day_type'] = df_trend['dow'].apply(lambda x: '週末 (Weekend)' if x >= 5 else '工作日 (Weekday)')

        # 2. 進行分組計算
        daily_pattern = df_trend.groupby(['day_type', 'hour'])[selected_metric].median().reset_index()

        # 3. 繪製折線圖
        fig_line = px.line(
            daily_pattern,
            x='hour',
            y=selected_metric,
            color='day_type',  # 用顏色區分工作日和週末
            markers=True,
            labels={'hour': '小時 (Hour)', selected_metric: yaxis_title, 'day_type': '類別 (Day Type)'},
            template='plotly_dark'
        )
        
        # 4. 新增上午和下午的尖峰標示線
        fig_line.add_vline(x=8, line_width=2, line_dash="dash", line_color="grey", annotation_text="上午尖峰 (AM Peak)")
        fig_line.add_vline(x=17, line_width=2, line_dash="dash", line_color="grey", annotation_text="下午尖峰 (PM Peak)")
        
        fig_line.update_layout(
            height=500,
            title_text='每日趨勢比較：工作日 vs. 週末',
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        st.plotly_chart(fig_line, use_container_width=True)
    else:
        st.info("缺少 'dow', 'hour', 或選擇的指標欄位，無法繪製每日趨勢圖。")

    st.markdown("---")
    
    # ----- 圖表二：優化版熱力圖 -----
    st.subheader("優化版熱力圖 (增加數值與對比色)")

    if {'dow', 'hour', 'average_speed'}.issubset(_df.columns):
        pivot = _df.pivot_table(index='dow', columns='hour', values='average_speed', aggfunc='median')
        pivot.index = ['週一 (Mon)', '週二 (Tue)', '週三 (Wed)', '週四 (Thu)', '週五 (Fri)', '週六 (Sat)', '週日 (Sun)'][:len(pivot.index)]
        
        # 使用更有意義的顏色，並顯示數值
        fig_heatmap = px.imshow(
            pivot,
            text_auto=True,  # 在格子上顯示數值
            aspect='auto',
            color_continuous_scale=px.colors.diverging.RdYlGn, # 使用 紅-黃-綠 色階 (低速紅, 高速綠)
            labels=dict(color="中位數車速 (km/h)", x="小時 (Hour)", y="星期 (Day of Week)"),
            template='plotly_dark'
        )
        
        fig_heatmap.update_traces(textfont_size=10) # 調整格子內字體大小
        fig_heatmap.update_layout(
            height=520,
            title='每小時中位數車速熱力圖'
        )
        st.plotly_chart(fig_heatmap, use_container_width=True)
    else:
        st.info("缺少 'dow', 'hour', 或 'average_speed' 欄位，無法繪製熱力圖。")

# ---------- 相關性分析 ----------
with TAB_CORR:
    st.subheader("外部因素上傳與合併（天氣/活動/路況…）")
    st.caption("上傳 CSV（需有時間欄位），並選擇對齊粒度與相關方法，檢視對流量/速度的影響。")
    up = st.file_uploader("Upload external factors CSV", type=['csv'])
    if up is None:
        st.info("尚未上傳外部因素。")
    else:
        ext = pd.read_csv(up)
        ext_cols = list(ext.columns)
        dt_col = st.selectbox("Select datetime column in CSV", ext_cols)
        try:
            ext[dt_col] = pd.to_datetime(ext[dt_col], errors='coerce')
        except Exception:
            st.error("無法解析時間欄位，請確認格式。")
            st.stop()
        ext = ext.dropna(subset=[dt_col]).sort_values(dt_col)

        c1, c2 = st.columns(2)
        freq = c1.selectbox("Align by", ["Hourly", "Daily"], index=0)
        method = c2.selectbox("Correlation method", ["pearson", "spearman"], index=0)

        rule = 'H' if freq == "Hourly" else 'D'
        base = (_df.set_index('datetime').sort_index()
                  .resample(rule).agg({'traffic_volume': 'mean', 'average_speed': 'mean', 'incidents': 'sum'}))
        ext_r = (ext.set_index(dt_col).sort_index().resample(rule).mean())
        join = base.join(ext_r, how='inner')

        num_cols = join.select_dtypes(include=[np.number]).columns.tolist()
        exclude = ['traffic_volume', 'average_speed', 'incidents']
        ext_feats = [c for c in num_cols if c not in exclude]
        if not ext_feats:
            st.warning("找不到可用的數值型外部特徵。")
        else:
            corr_v = join[ext_feats + ['traffic_volume']].corr(method=method)['traffic_volume'].drop('traffic_volume')
            corr_s = join[ext_feats + ['average_speed']].corr(method=method)['average_speed'].drop('average_speed')
            b1, b2 = st.columns(2)
            with b1:
                fig = px.bar(corr_v.sort_values(), x=corr_v.sort_values().values, y=corr_v.sort_values().index,
                             orientation='h', title=f'Correlation with Traffic Volume ({method})', labels={'x': 'corr', 'y': ''})
                fig.update_layout(height=460)
                st.plotly_chart(fig, use_container_width=True)
            with b2:
                fig = px.bar(corr_s.sort_values(), x=corr_s.sort_values().values, y=corr_s.sort_values().index,
                             orientation='h', title=f'Correlation with Average Speed ({method})', labels={'x': 'corr', 'y': ''})
                fig.update_layout(height=460)
                st.plotly_chart(fig, use_container_width=True)

            st.markdown("---")
            st.subheader("關係檢視（含趨勢線）")
            y_target = st.selectbox("Target", ['traffic_volume', 'average_speed'], index=0)
            x_feat = st.selectbox("External feature", ext_feats, index=0)
            fig = px.scatter(join.reset_index(), x=x_feat, y=y_target, trendline='ols', opacity=0.6,
                             title=f"{x_feat} vs {y_target} ({freq})")
            fig.update_layout(height=520)
            st.plotly_chart(fig, use_container_width=True)

            st.markdown("---")
            st.subheader("合併後資料 (for download)")
            st.dataframe(join.reset_index().head(1000))
            st.download_button("Download merged CSV", data=to_csv_bytes(join.reset_index()),
                               file_name=f"merged_{freq.lower()}_external.csv")
