from __future__ import annotations
import json, re, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import duckdb, pandas as pd
from fastapi import UploadFile
from app.config import settings
from app.schemas import ColumnStat, DatasetMeta

ALLOWED_EXTENSIONS={".csv",".tsv",".json",".parquet",".xlsx"}

def now_iso(): return datetime.now(timezone.utc).isoformat()
def safe_filename(name:str)->str:
    name=Path(name).name
    name=re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]+","_",name)
    return name[:160] or "uploaded_file"
def dataset_meta_path(dataset_id:str)->Path: return settings.dataset_meta_dir/f"{dataset_id}.json"
def save_meta(meta:DatasetMeta)->None:
    settings.dataset_meta_dir.mkdir(parents=True,exist_ok=True)
    dataset_meta_path(meta.datasetId).write_text(meta.model_dump_json(indent=2),encoding="utf-8")
def load_meta(dataset_id:str)->DatasetMeta:
    p=dataset_meta_path(dataset_id)
    if not p.exists(): raise FileNotFoundError("数据集不存在。")
    return DatasetMeta.model_validate_json(p.read_text(encoding="utf-8"))
def list_metas()->list[DatasetMeta]:
    settings.dataset_meta_dir.mkdir(parents=True,exist_ok=True)
    out=[]
    for p in sorted(settings.dataset_meta_dir.glob("*.json"),reverse=True):
        try: out.append(DatasetMeta.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception: pass
    return out
async def save_upload(file:UploadFile)->Path:
    settings.upload_dir.mkdir(parents=True,exist_ok=True)
    original=file.filename or "uploaded"; ext=Path(original).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS: raise ValueError(f"不支持的文件格式：{ext}")
    max_bytes=settings.max_upload_mb*1024*1024
    dataset_id=uuid.uuid4().hex
    stored=settings.upload_dir/f"{dataset_id}_{safe_filename(original)}"
    size=0
    with stored.open("wb") as out:
        while True:
            chunk=await file.read(1024*1024)
            if not chunk: break
            size+=len(chunk)
            if size>max_bytes:
                try: stored.unlink()
                except Exception: pass
                raise ValueError(f"文件超过限制：{settings.max_upload_mb} MB")
            out.write(chunk)
    return stored
def q(name:str)->str: return '"' + name.replace('"','""') + '"'
def san(v:Any)->Any:
    try:
        if pd.isna(v): return None
    except Exception: pass
    if hasattr(v,"isoformat"): return v.isoformat()
    if isinstance(v,(int,float,str,bool)) or v is None: return v
    return str(v)
def load_file_to_duckdb(conn,path:Path,table_name="data_table"):
    ext=path.suffix.lower(); conn.execute(f"DROP TABLE IF EXISTS {q(table_name)}")
    if ext==".csv": conn.read_csv(str(path),header=True,auto_detect=True).create(table_name); return
    if ext==".tsv": conn.read_csv(str(path),header=True,auto_detect=True,sep="\\t").create(table_name); return
    if ext==".parquet": conn.read_parquet(str(path)).create(table_name); return
    if ext==".json": conn.read_json(str(path),auto_detect=True).create(table_name); return
    if ext==".xlsx":
        df=pd.read_excel(path); conn.register("_xlsx_df",df)
        conn.execute(f"CREATE TABLE {q(table_name)} AS SELECT * FROM _xlsx_df"); conn.unregister("_xlsx_df"); return
    raise ValueError(f"不支持的文件格式：{ext}")
def build_dataset_meta(path:Path,original_filename:str)->DatasetMeta:
    dataset_id=path.name.split("_",1)[0]; table="data_table"; conn=duckdb.connect(database=":memory:")
    try:
        load_file_to_duckdb(conn,path,table)
        row_count=int(conn.execute(f"SELECT COUNT(*) FROM {q(table)}").fetchone()[0])
        info=conn.execute(f"PRAGMA table_info('{table}')").fetchall()
        cols=[]
        for _,name,col_type,*_ in info:
            qq=q(name)
            null_count=int(conn.execute(f"SELECT COUNT(*)-COUNT({qq}) FROM {q(table)}").fetchone()[0])
            try: distinct=int(conn.execute(f"SELECT COUNT(DISTINCT {qq}) FROM {q(table)}").fetchone()[0])
            except Exception: distinct=None
            try:
                ex=conn.execute(f"SELECT DISTINCT {qq} FROM {q(table)} WHERE {qq} IS NOT NULL LIMIT 5").fetchall()
                examples=[san(r[0]) for r in ex]
            except Exception: examples=[]
            stat=ColumnStat(name=name,type=col_type,nullCount=null_count,nullRatio=(null_count/row_count) if row_count else 0,distinctCount=distinct,examples=examples)
            cols.append(stat)
        preview=conn.execute(f"SELECT * FROM {q(table)} LIMIT 10").fetchdf()
        rows=[{str(k):san(v) for k,v in row.items()} for row in preview.to_dict(orient="records")]
        meta=DatasetMeta(datasetId=dataset_id,originalFilename=original_filename,storedPath=str(path),extension=path.suffix.lower(),tableName=table,uploadedAt=now_iso(),rowCount=row_count,columns=cols,previewRows=rows)
        save_meta(meta); return meta
    finally: conn.close()
FORBIDDEN_SQL=re.compile(r"\\b(insert|update|delete|drop|alter|create|copy|export|install|load|attach|detach|pragma|call|merge|truncate|grant|revoke)\\b",re.I)
def strip_sql(sql:str)->str:
    sql=sql.strip(); sql=re.sub(r"^```(?:sql|json)?\\s*","",sql,flags=re.I); sql=re.sub(r"\\s*```$","",sql).strip()
    return sql[:-1].strip() if sql.endswith(";") else sql
def validate_select_sql(sql:str)->str:
    cleaned=strip_sql(sql); lowered=cleaned.lower().lstrip()
    if not (lowered.startswith("select") or lowered.startswith("with")): raise ValueError("SQL 安全校验失败：只允许 SELECT 或 WITH 查询。")
    if ";" in cleaned: raise ValueError("SQL 安全校验失败：不允许多语句。")
    if FORBIDDEN_SQL.search(cleaned): raise ValueError("SQL 安全校验失败：包含禁止的 SQL 关键字。")
    return cleaned
def add_limit_if_needed(sql:str,limit:int)->str:
    cleaned=validate_select_sql(sql)
    if re.search(r"\\blimit\\s+\\d+\\b",cleaned,flags=re.I): return cleaned
    return f"SELECT * FROM ({cleaned}) AS __limited_result LIMIT {limit}"
def execute_select_query(meta:DatasetMeta,sql:str,limit:int):
    limit=max(1,min(limit,settings.max_result_rows)); conn=duckdb.connect(database=":memory:")
    try:
        load_file_to_duckdb(conn,Path(meta.storedPath),meta.tableName)
        result=conn.execute(add_limit_if_needed(sql,limit)); columns=[d[0] for d in result.description] if result.description else []
        rows=result.fetchmany(limit); data=[{str(c):san(v) for c,v in zip(columns,row)} for row in rows]
        return [str(c) for c in columns],data
    finally: conn.close()
def schema_for_prompt(meta:DatasetMeta):
    return {"table_name":meta.tableName,"row_count":meta.rowCount,"columns":[{"name":c.name,"type":c.type,"examples":c.examples,"null_ratio":round(c.nullRatio,4)} for c in meta.columns],"preview_rows":meta.previewRows[:5]}
def build_text_to_sql_prompt(meta:DatasetMeta,question:str,limit:int)->str:
    return f"""你是 DuckDB SQL 数据分析助手。只能输出 JSON：{{"sql":"SELECT ...","explain":"中文解释"}}。
只允许 SELECT 或 WITH SELECT；字段名有特殊字符必须双引号；LIMIT 不超过 {limit}。
表名：{meta.tableName}
Schema:
{json.dumps(schema_for_prompt(meta),ensure_ascii=False,indent=2)}
用户问题：{question}
"""
def parse_model_sql_answer(answer:str):
    text=answer.strip(); text=re.sub(r"^```(?:json)?\\s*","",text,flags=re.I); text=re.sub(r"\\s*```$","",text).strip()
    m=re.search(r"\\{.*\\}",text,flags=re.S)
    if m: text=m.group(0)
    data=json.loads(text); sql=str(data.get("sql") or "").strip(); explain=str(data.get("explain") or "").strip()
    if not sql: raise ValueError("模型返回中没有 sql 字段。")
    return validate_select_sql(sql), explain
