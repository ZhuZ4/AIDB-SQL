import json, os
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer, select, text
from sqlalchemy.engine import Engine
from llama_index.core import SQLDatabase
from .utils import read_json, write_json, save_raw_text, examples_to_str
from .m_schema import MSchema


class SchemaEngine(SQLDatabase):
    def __init__(self, engine: Engine, schema: Optional[str] = None, metadata: Optional[MetaData] = None,
                 ignore_tables: Optional[List[str]] = None, include_tables: Optional[List[str]] = None,
                 sample_rows_in_table_info: int = 8, indexes_in_table_info: bool = False,
                 custom_table_info: Optional[dict] = None, view_support: bool = False, max_string_length: int = 300,
                 mschema: Optional[MSchema] = None, db_name: Optional[str] = ''):
                         
        # 新增：前缀过滤配置
        self._clean_prefixes = ['information_schema', 'sql_', 'pg_', 'system']

        super().__init__(engine, schema, metadata, ignore_tables, include_tables, sample_rows_in_table_info,
                         indexes_in_table_info, custom_table_info, view_support, max_string_length)

        self.foreign_keys = ""
        self._db_name = db_name
        # Dictionary to store table names and their corresponding schema
        self._tables_schemas: Dict[str, str] = {}  # 表名到schema的映射

        # 新增：表名清洗函数
        def clean_table_name(name: str) -> str:
            """去除表名中的多余前缀"""
            # 剥离所有模式前缀
            if '.' in name:
                parts = name.split('.')
                # 保留最后一个有效部分
                return parts[-1] if parts[-1] != 'public' else parts[-2]
            return name
        
        # If a schema is specified, filter by that schema and store that value for every table.
        if schema:
            # 获取指定schema下的原始表名
            raw_tables = self.get_table_names()

            # 清洗表名
            cleaned_tables = [
                clean_table_name(table_name) 
                for table_name in raw_tables
                if self._inspector.has_table(table_name, schema)
            ]
            # 过滤系统表
            self._usable_tables = [
                t for t in cleaned_tables
                if not any(t.startswith(p) for p in self._clean_prefixes)
            ]

            # self._usable_tables = [
            #     # 新增：清洗表名
            #     clean_table_name(table_name) for table_name in self._usable_tables
            #     # table_name for table_name in self._usable_tables
            #     if self._inspector.has_table(table_name, schema)
            # ]

            # 存储schema信息
            for table_name in self._usable_tables:
                self._tables_schemas[table_name] = schema
        else:
            all_tables = []
            # Iterate through all available schemas
            for s in self.get_schema_names():

                # 跳过系统schema
                if s in self._clean_prefixes:
                    continue

                # 获取原始表名
                raw_tables = self._inspector.get_table_names(schema=s)

                # 清洗表名
                clean_tables = [clean_table_name(t) for t in raw_tables]

                # 过滤系统表
                valid_tables = [
                    t for t in clean_tables
                    if not any(t.startswith(p) for p in self._clean_prefixes)
                ]

                all_tables.extend(valid_tables)

                for table in valid_tables:  
                    self._tables_schemas[table] = s
            self._usable_tables = all_tables

        self._dialect = engine.dialect.name
        if mschema is not None:
            self._mschema = mschema
        else:
            self._mschema = MSchema(db_id=db_name, schema=schema)
            self.init_mschema()

    @property
    def mschema(self) -> MSchema:
        """Return M-Schema"""
        return self._mschema
    
    # ==== 新增方法：get_table_names ====
    def get_table_names(self) -> List[str]:
        """重写获取表名方法，返回已清洗的表名列表"""
        return list(self._tables_schemas.keys())
    
    # 新增：设置前缀过滤的方法
    def set_clean_prefixes(self, prefixes: List[str]):
        """设置需要过滤的表名前缀"""
        self._clean_prefixes = prefixes

    def get_pk_constraint(self, table_name: str) -> Dict:
        """重写主键约束获取方法，使用_tables_schemas中的schema"""
        schema = self._tables_schemas.get(table_name)
        if not schema:
            return []
        return self._inspector.get_pk_constraint(table_name, schema)['constrained_columns']
    # def get_pk_constraint(self, table_name: str) -> Dict:
    #     return self._inspector.get_pk_constraint(table_name, self._tables_schemas[table_name] )['constrained_columns']

    # def get_table_comment(self, table_name: str):
    #     try:
    #         return self._inspector.get_table_comment(table_name, self._tables_schemas[table_name])['text']
    #     except:    # sqlite does not support comments
    #         return ''

    # ==== 修改：get_table_comment方法中获取schema的方式 ====
    def get_table_comment(self, table_name: str):
        try:
            # 使用_tables_schemas中的schema
            schema = self._tables_schemas[table_name]
            return self._inspector.get_table_comment(table_name, schema)['text']
        except:  # sqlite does not support comments
            return ''

    def default_schema_name(self) -> Optional[str]:
        return self._inspector.default_schema_name

    def get_schema_names(self) -> List[str]:
        return self._inspector.get_schema_names()
    

    def get_foreign_keys(self, table_name: str):
        """重写外键获取方法，使用_tables_schemas中的schema"""
        schema = self._tables_schemas.get(table_name)
        if not schema:
            return []
        return self._inspector.get_foreign_keys(table_name, schema)
    
    # def get_foreign_keys(self, table_name: str):
    #     """重写外键获取方法，使用_tables_schemas中的schema"""
    #     schema = self._tables_schemas.get(table_name)
    #     if not schema:
    #         return []
    #     return self._inspector.get_foreign_keys(table_name, schema)
    # def get_foreign_keys(self, table_name: str):
    #     return self._inspector.get_foreign_keys(table_name, self._tables_schemas[table_name])

    def get_unique_constraints(self, table_name: str):
        return self._inspector.get_unique_constraints(table_name, self._tables_schemas[table_name])

    def fectch_distinct_values(self, table_name: str, column_name: str, max_num: int = 5):
        """重写获取示例值方法，使用_tables_schemas中的schema"""
        schema = self._tables_schemas.get(table_name)
        if not schema:
            return []
            
        try:
            table = Table(table_name, self.metadata_obj, autoload_with=self._engine, schema=schema)
            # 构造SELECT DISTINCT查询
            query = select(table.c[column_name]).distinct().limit(max_num)
            values = []
            with self._engine.connect() as connection:
                result = connection.execute(query)
                distinct_values = result.fetchall()
                for value in distinct_values:
                    if value[0] is not None and value[0] != '':
                        values.append(value[0])
            return values
        except Exception as e:
            print(f"获取{table_name}.{column_name}示例值时出错: {str(e)}")
            return []
    # def fectch_distinct_values(self, table_name: str, column_name: str, max_num: int = 5):
    #     table = Table(table_name, self.metadata_obj, autoload_with=self._engine, schema=self._tables_schemas[table_name])
    #     # Construct SELECT DISTINCT query
    #     query = select(table.c[column_name]).distinct().limit(max_num)
    #     values = []
    #     with self._engine.connect() as connection:
    #         result = connection.execute(query)
    #         distinct_values = result.fetchall()
    #         for value in distinct_values:
    #             if value[0] is not None and value[0] != '':
    #                 values.append(value[0])
    #     return values

    def init_mschema(self):
        # 存储所有外键关系，用于后续格式化输出
        foreign_key_relations = []

        for table_name in self._usable_tables:
            table_comment = self.get_table_comment(table_name)
            table_comment = '' if table_comment is None else table_comment.strip()
            # table_with_schema = self._tables_schemas[table_name] + '.' + table_name
            # self._mschema.add_table(table_with_schema, fields={}, comment=table_comment)

            # 添加到mschema
            self._mschema.add_table(table_name, fields={}, comment=table_comment)
            pks = self.get_pk_constraint(table_name)

            fks = self.get_foreign_keys(table_name)
            for fk in fks:
                referred_schema = fk['referred_schema']
                referred_table = fk['referred_table']

                # 清洗关联表名（如果包含前缀）
                if '.' in referred_table:
                    referred_table = referred_table.split('.')[-1]

                # for c, r in zip(fk['constrained_columns'], fk['referred_columns']):
                #     self._mschema.add_foreign_key(table_name, c, referred_schema, referred_table, r)
                # 收集外键关系用于格式化输出
                for c, r in zip(fk['constrained_columns'], fk['referred_columns']):
                    # 添加外键到mschema
                    self._mschema.add_foreign_key(table_name, c, referred_schema, referred_table, r)
                    
                    # 存储关系用于格式化输出
                    relation = f"{table_name}.{c}={referred_table}.{r}"
                    foreign_key_relations.append(relation)

            # 获取表字段信息
            schema = self._tables_schemas[table_name]
            fields = self._inspector.get_columns(table_name, schema=schema)
            # fields = self._inspector.get_columns(table_name, schema=self._tables_schemas[table_name])
            for field in fields:
                field_type = f"{field['type']!s}"
                field_name = field['name']
                primary_key = field_name in pks
                field_comment = field.get("comment", None)
                field_comment = "" if field_comment is None else field_comment.strip()
                autoincrement = field.get('autoincrement', False)
                default = field.get('default', None)
                if default is not None:
                    default = f'{default}'

                # 尝试获取示例值
                try:
                    examples = self.fectch_distinct_values(table_name, field_name, 8)
                except Exception as e:
                    # 捕获异常避免中断，使用空列表
                    print(f"获取{table_name}.{field_name}示例值时出错: {str(e)}")
                    examples = []
                examples = examples_to_str(examples)

                self._mschema.add_field(
                    table_name, field_name, field_type=field_type, primary_key=primary_key,
                    nullable=field['nullable'], default=default, autoincrement=autoincrement,
                    comment=field_comment, examples=examples
                )
        # 设置外键关系格式化输出
        self._mschema.set_foreign_keys("\n".join(foreign_key_relations))