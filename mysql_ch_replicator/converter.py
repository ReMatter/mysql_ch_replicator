import struct
import json
import uuid
import sqlparse
import re
from pyparsing import Suppress, CaselessKeyword, Word, alphas, alphanums, delimitedList

from .table_structure import TableStructure, TableField
from .enum import (
    parse_mysql_enum, EnumConverter,
    parse_enum_or_set_field,
    extract_enum_or_set_values
)


CHARSET_MYSQL_TO_PYTHON = {
    'armscii8': None,          # ARMSCII-8 is not directly supported in Python
    'ascii': 'ascii',
    'big5': 'big5',
    'binary': 'latin1',        # Treat binary data as Latin-1 in Python
    'cp1250': 'cp1250',
    'cp1251': 'cp1251',
    'cp1256': 'cp1256',
    'cp1257': 'cp1257',
    'cp850': 'cp850',
    'cp852': 'cp852',
    'cp866': 'cp866',
    'cp932': 'cp932',
    'dec8': 'latin1',          # DEC8 is similar to Latin-1
    'eucjpms': 'euc_jp',       # Map to EUC-JP
    'euckr': 'euc_kr',
    'gb18030': 'gb18030',
    'gb2312': 'gb2312',
    'gbk': 'gbk',
    'geostd8': None,           # GEOSTD8 is not directly supported in Python
    'greek': 'iso8859_7',
    'hebrew': 'iso8859_8',
    'hp8': None,               # HP8 is not directly supported in Python
    'keybcs2': None,           # KEYBCS2 is not directly supported in Python
    'koi8r': 'koi8_r',
    'koi8u': 'koi8_u',
    'latin1': 'cp1252',        # MySQL's latin1 corresponds to Windows-1252
    'latin2': 'iso8859_2',
    'latin5': 'iso8859_9',
    'latin7': 'iso8859_13',
    'macce': 'mac_latin2',
    'macroman': 'mac_roman',
    'sjis': 'shift_jis',
    'swe7': None,              # SWE7 is not directly supported in Python
    'tis620': 'tis_620',
    'ucs2': 'utf_16',          # UCS-2 can be mapped to UTF-16
    'ujis': 'euc_jp',
    'utf16': 'utf_16',
    'utf16le': 'utf_16_le',
    'utf32': 'utf_32',
    'utf8mb3': 'utf_8',        # Both utf8mb3 and utf8mb4 can be mapped to UTF-8
    'utf8mb4': 'utf_8',
    'utf8': 'utf_8',
}


def convert_bytes(obj):
    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            new_key = k.decode('utf-8') if isinstance(k, bytes) else k
            new_value = convert_bytes(v)
            new_obj[new_key] = new_value
        return new_obj
    elif isinstance(obj, (tuple, list)):
        new_obj = []
        for item in obj:
            new_obj.append(convert_bytes(item))
        if isinstance(obj, tuple):
            return tuple(new_obj)
        return new_obj
    elif isinstance(obj, bytes):
        return obj.decode('utf-8')
    else:
        return obj


def parse_mysql_point(binary):
    """
    Parses the binary representation of a MySQL POINT data type
    and returns a tuple (x, y) representing the coordinates.

    :param binary: The binary data representing the POINT.
    :return: A tuple (x, y) with the coordinate values.
    """
    if binary is None:
        return 0, 0

    if len(binary) == 21:
        # No SRID. Proceed as per WKB POINT
        # Read the byte order
        byte_order = binary[0]
        if byte_order == 0:
            endian = '>'
        elif byte_order == 1:
            endian = '<'
        else:
            raise ValueError("Invalid byte order in WKB POINT")
        # Read the WKB Type
        wkb_type = struct.unpack(endian + 'I', binary[1:5])[0]
        if wkb_type != 1:  # WKB type 1 means POINT
            raise ValueError("Not a WKB POINT type")
        # Read X and Y coordinates
        x = struct.unpack(endian + 'd', binary[5:13])[0]
        y = struct.unpack(endian + 'd', binary[13:21])[0]
    elif len(binary) == 25:
        # With SRID included
        # First 4 bytes are the SRID
        srid = struct.unpack('>I', binary[0:4])[0]  # SRID is big-endian
        # Next byte is byte order
        byte_order = binary[4]
        if byte_order == 0:
            endian = '>'
        elif byte_order == 1:
            endian = '<'
        else:
            raise ValueError("Invalid byte order in WKB POINT")
        # Read the WKB Type
        wkb_type = struct.unpack(endian + 'I', binary[5:9])[0]
        if wkb_type != 1:  # WKB type 1 means POINT
            raise ValueError("Not a WKB POINT type")
        # Read X and Y coordinates
        x = struct.unpack(endian + 'd', binary[9:17])[0]
        y = struct.unpack(endian + 'd', binary[17:25])[0]
    else:
        raise ValueError("Invalid binary length for WKB POINT")
    return (x, y)


def strip_sql_name(name):
    name = name.strip()
    if name.startswith('`'):
        name = name[1:]
    if name.endswith('`'):
        name = name[:-1]
    return name


def split_high_level(data, token):
    results = []
    level = 0
    curr_data = ''
    for c in data:
        if c == token and level == 0:
            results.append(curr_data.strip())
            curr_data = ''
            continue
        if c == '(':
            level += 1
        if c == ')':
            level -= 1
        curr_data += c
    if curr_data:
        results.append(curr_data.strip())
    return results


def strip_sql_comments(sql_statement):
    return sqlparse.format(sql_statement, strip_comments=True).strip()


def convert_timestamp_to_datetime64(input_str):

    # Define the regex pattern
    pattern = r'^timestamp(?:\((\d+)\))?$'

    # Attempt to match the pattern
    match = re.match(pattern, input_str.strip(), re.IGNORECASE)

    if match:
        # If a precision is provided, include it in the replacement
        precision = match.group(1)
        if precision is not None:
            return f'DateTime64({precision})'
        else:
            return 'DateTime64'
    else:
        raise ValueError(f"Invalid input string format: '{input_str}'")


class MysqlToClickhouseConverter:
    def __init__(self, db_replicator: 'DbReplicator' = None):
        self.db_replicator = db_replicator
        self.types_mapping = {}
        if self.db_replicator is not None:
            self.types_mapping = db_replicator.config.types_mapping

    def convert_type(self, mysql_type, parameters):
        is_unsigned = 'unsigned' in parameters.lower()

        result_type = self.types_mapping.get(mysql_type)
        if result_type is not None:
            return result_type

        if mysql_type == 'point':
            return 'Tuple(x Float32, y Float32)'

        # Correctly handle numeric types
        if mysql_type.startswith('numeric'):
            # Determine if parameters are specified via parentheses:
            if '(' in mysql_type and ')' in mysql_type:
                # Expecting a type definition like "numeric(precision, scale)"
                pattern = r"numeric\((\d+)\s*,\s*(\d+)\)"
                match = re.search(pattern, mysql_type)
                if not match:
                    raise ValueError(f"Invalid numeric type definition: {mysql_type}")

                precision = int(match.group(1))
                scale = int(match.group(2))
            else:
                # If no parentheses are provided, assume defaults.
                precision = 10  # or other default as defined by your standards
                scale = 0

            # If no fractional part, consider mapping to integer type (if desired)
            if scale == 0:
                if is_unsigned:
                    if precision <= 9:
                        return "UInt32"
                    elif precision <= 18:
                        return "UInt64"
                    else:
                        # For very large precisions, fallback to Decimal
                        return f"Decimal({precision}, {scale})"
                else:
                    if precision <= 9:
                        return "Int32"
                    elif precision <= 18:
                        return "Int64"
                    else:
                        return f"Decimal({precision}, {scale})"
            else:
                # For types with a defined fractional part, use a Decimal mapping.
                return f"Decimal({precision}, {scale})"

        if mysql_type == 'int':
            if is_unsigned:
                return 'UInt32'
            return 'Int32'
        if mysql_type == 'integer':
            if is_unsigned:
                return 'UInt32'
            return 'Int32'
        if mysql_type == 'bigint':
            if is_unsigned:
                return 'UInt64'
            return 'Int64'
        if mysql_type == 'double':
            return 'Float64'
        if mysql_type == 'real':
            return 'Float64'
        if mysql_type == 'float':
            return 'Float32'
        if mysql_type == 'date':
            return 'Date32'
        if mysql_type == 'tinyint(1)':
            return 'Bool'
        if mysql_type == 'bit(1)':
            return 'Bool'
        if mysql_type == 'bool':
            return 'Bool'
        if 'smallint' in mysql_type:
            if is_unsigned:
                return 'UInt16'
            return 'Int16'
        if 'tinyint' in mysql_type:
            if is_unsigned:
                return 'UInt8'
            return 'Int8'
        if 'mediumint' in mysql_type:
            if is_unsigned:
                return 'UInt32'
            return 'Int32'
        if 'datetime' in mysql_type:
            return mysql_type.replace('datetime', 'DateTime64')
        if 'longtext' in mysql_type:
            return 'String'
        if 'varchar' in mysql_type:
            return 'String'
        if mysql_type.startswith('enum'):
            enum_values = parse_mysql_enum(mysql_type)
            ch_enum_values = []
            for idx, value_name in enumerate(enum_values):
                ch_enum_values.append(f"'{value_name.lower()}' = {idx+1}")
            ch_enum_values = ', '.join(ch_enum_values)
            if len(enum_values) <= 127:
                # Enum8('red' = 1, 'green' = 2, 'black' = 3)
                return f'Enum8({ch_enum_values})'
            else:
                # Enum16('red' = 1, 'green' = 2, 'black' = 3)
                return f'Enum16({ch_enum_values})'
        if 'text' in mysql_type:
            return 'String'
        if 'blob' in mysql_type:
            return 'String'
        if 'char' in mysql_type:
            return 'String'
        if 'json' in mysql_type:
            return 'String'
        if 'decimal' in mysql_type:
            return 'Float64'
        if 'float' in mysql_type:
            return 'Float32'
        if 'double' in mysql_type:
            return 'Float64'
        if 'bigint' in mysql_type:
            if is_unsigned:
                return 'UInt64'
            return 'Int64'
        if 'integer' in mysql_type or 'int(' in mysql_type:
            if is_unsigned:
                return 'UInt32'
            return 'Int32'
        if 'real' in mysql_type:
            return 'Float64'
        if mysql_type.startswith('timestamp'):
            return convert_timestamp_to_datetime64(mysql_type)
        if mysql_type.startswith('time'):
            return 'String'
        if 'varbinary' in mysql_type:
            return 'String'
        if 'binary' in mysql_type:
            return 'String'
        if 'set(' in mysql_type:
            return 'String'
        raise Exception(f'unknown mysql type "{mysql_type}"')

    def convert_field_type(self, mysql_type, mysql_parameters):
        mysql_type = mysql_type.lower()
        mysql_parameters = mysql_parameters.lower()
        not_null = 'not null' in mysql_parameters
        clickhouse_type = self.convert_type(mysql_type, mysql_parameters)
        if 'Tuple' in clickhouse_type:
            not_null = True
        if not not_null:
            clickhouse_type = f'Nullable({clickhouse_type})'
        return clickhouse_type

    def convert_table_structure(self, mysql_structure: TableStructure) -> TableStructure:
        clickhouse_structure = TableStructure()
        clickhouse_structure.table_name = mysql_structure.table_name
        clickhouse_structure.if_not_exists = mysql_structure.if_not_exists
        for field in mysql_structure.fields:
            clickhouse_field_type = self.convert_field_type(field.field_type, field.parameters)
            clickhouse_structure.fields.append(TableField(
                name=field.name,
                field_type=clickhouse_field_type,
            ))
        clickhouse_structure.primary_keys = mysql_structure.primary_keys
        clickhouse_structure.preprocess()
        return clickhouse_structure

    def convert_records(
            self, mysql_records, mysql_structure: TableStructure, clickhouse_structure: TableStructure,
            only_primary: bool = False,
    ):
        mysql_field_types = [field.field_type for field in mysql_structure.fields]
        clickhouse_filed_types = [field.field_type for field in clickhouse_structure.fields]

        clickhouse_records = []
        for mysql_record in mysql_records:
            clickhouse_record = self.convert_record(
                mysql_record, mysql_field_types, clickhouse_filed_types, mysql_structure, only_primary,
            )
            clickhouse_records.append(clickhouse_record)
        return clickhouse_records

    def convert_record(
            self, mysql_record, mysql_field_types, clickhouse_field_types, mysql_structure: TableStructure,
            only_primary: bool,
    ):
        clickhouse_record = []
        for idx, mysql_field_value in enumerate(mysql_record):
            if only_primary and idx not in mysql_structure.primary_key_ids:
                clickhouse_record.append(mysql_field_value)
                continue

            clickhouse_field_value = mysql_field_value
            try:
                mysql_field_type = mysql_field_types[idx]
            except IndexError:
                field_count = len(mysql_field_types)
                record_fields = len(mysql_record)
                table_name = getattr(mysql_structure, 'table_name', 'unknown')
                raise IndexError(
                    f"Field index {idx} out of range for table '{table_name}' with field_value: {mysql_field_value}. "
                    f"Record has {record_fields} fields but schema has {field_count} fields. "
                    f"Field types defined in schema: {mysql_field_types}. "
                    f"This may indicate a schema mismatch between MySQL and the replication process."
                )
            clickhouse_field_type = clickhouse_field_types[idx]    
            if mysql_field_type.startswith('time') and 'String' in clickhouse_field_type:
                clickhouse_field_value = str(mysql_field_value)
            if mysql_field_type == 'json' and 'String' in clickhouse_field_type:
                if not isinstance(clickhouse_field_value, str):
                    clickhouse_field_value = json.dumps(convert_bytes(clickhouse_field_value))

            if clickhouse_field_value is not None:
                if 'UUID' in clickhouse_field_type:
                    if len(clickhouse_field_value) == 36:
                        if isinstance(clickhouse_field_value, bytes):
                            clickhouse_field_value = clickhouse_field_value.decode('utf-8')
                        clickhouse_field_value = uuid.UUID(clickhouse_field_value).bytes

                if 'UInt16' in clickhouse_field_type and clickhouse_field_value < 0:
                    clickhouse_field_value = 65536 + clickhouse_field_value
                if 'UInt8' in clickhouse_field_type and clickhouse_field_value < 0:
                    clickhouse_field_value = 256 + clickhouse_field_value
                if 'mediumint' in mysql_field_type.lower() and clickhouse_field_value < 0:
                    clickhouse_field_value = 16777216 + clickhouse_field_value
                if 'UInt32' in clickhouse_field_type and clickhouse_field_value < 0:
                    clickhouse_field_value = 4294967296 + clickhouse_field_value
                if 'UInt64' in clickhouse_field_type and clickhouse_field_value < 0:
                    clickhouse_field_value = 18446744073709551616 + clickhouse_field_value

                if 'String' in clickhouse_field_type and (
                        'text' in mysql_field_type or 'char' in mysql_field_type
                ):
                    if isinstance(clickhouse_field_value, bytes):
                        charset = mysql_structure.charset_python or 'utf-8'
                        clickhouse_field_value = clickhouse_field_value.decode(charset)

                if 'set(' in mysql_field_type:
                    set_values = mysql_structure.fields[idx].additional_data
                    if isinstance(clickhouse_field_value, int):
                        bit_mask = clickhouse_field_value
                        clickhouse_field_value = [
                            val
                            for idx, val in enumerate(set_values)
                            if bit_mask & (1 << idx)
                        ]
                    elif isinstance(clickhouse_field_value, set):
                        clickhouse_field_value = [
                            v for v in set_values if v in clickhouse_field_value
                        ]
                    clickhouse_field_value = ','.join(clickhouse_field_value)

            if mysql_field_type.startswith('point'):
                clickhouse_field_value = parse_mysql_point(clickhouse_field_value)

            if mysql_field_type.startswith('enum('):
                enum_values = mysql_structure.fields[idx].additional_data
                field_name = mysql_structure.fields[idx].name if idx < len(mysql_structure.fields) else "unknown"
                
                clickhouse_field_value = EnumConverter.convert_mysql_to_clickhouse_enum(
                    clickhouse_field_value, 
                    enum_values,
                    field_name
                )

            clickhouse_record.append(clickhouse_field_value)
        return tuple(clickhouse_record)

    def __basic_validate_query(self, mysql_query):
        mysql_query = mysql_query.strip()
        if mysql_query.endswith(';'):
            mysql_query = mysql_query[:-1]
        if mysql_query.find(';') != -1:
            raise Exception('multi-query statement not supported')
        return mysql_query
    
    def get_db_and_table_name(self, token, db_name):
        if '.' in token:
            db_name, table_name = token.split('.')
        else:
            table_name = token
        db_name = strip_sql_name(db_name)
        table_name = strip_sql_name(table_name)
        if self.db_replicator:
            if db_name == self.db_replicator.database:
                db_name = self.db_replicator.target_database
            matches_config = (
                self.db_replicator.config.is_database_matches(db_name)
                and self.db_replicator.config.is_table_matches(table_name))
        else:
            matches_config = True

        return db_name, table_name, matches_config

    def convert_alter_query(self, mysql_query, db_name):
        mysql_query = self.__basic_validate_query(mysql_query)

        tokens = mysql_query.split()
        if tokens[0].lower() != 'alter':
            raise Exception('wrong query')

        if tokens[1].lower() != 'table':
            raise Exception('wrong query')

        db_name, table_name, matches_config = self.get_db_and_table_name(tokens[2], db_name)

        if not matches_config:
            return

        subqueries = ' '.join(tokens[3:])
        subqueries = split_high_level(subqueries, ',')

        for subquery in subqueries:
            subquery = subquery.strip()
            tokens = subquery.split()

            op_name = tokens[0].lower()
            tokens = tokens[1:]

            if tokens[0].lower() == 'column':
                tokens = tokens[1:]

            if op_name == 'add':
                if tokens[0].lower() in ('constraint', 'index', 'foreign', 'unique'):
                    continue
                self.__convert_alter_table_add_column(db_name, table_name, tokens)
                continue

            if op_name == 'drop':
                if tokens[0].lower() in ('constraint', 'index', 'foreign', 'unique'):
                    continue
                self.__convert_alter_table_drop_column(db_name, table_name, tokens)
                continue

            if op_name == 'modify':
                self.__convert_alter_table_modify_column(db_name, table_name, tokens)
                continue

            if op_name == 'alter':
                continue

            if op_name == 'auto_increment':
                continue

            if op_name == 'change':
                self.__convert_alter_table_change_column(db_name, table_name, tokens)
                continue

            raise Exception(f'operation {op_name} not implement, query: {subquery}, full query: {mysql_query}')

    @classmethod
    def _tokenize_alter_query(cls, sql_line):
        # We want to recognize tokens that may be:
        # 1. A backquoted identifier that can optionally be immediately followed by parentheses.
        # 2. A plain word (letters/digits/underscore) that may immediately be followed by a parenthesized argument list.
        # 3. A single-quoted or double-quoted string.
        # 4. Or, if nothing else, any non‐whitespace sequence.
        #
        # The order is important: for example, if a word is immediately followed by parentheses,
        # we want to grab it as a single token.
        token_pattern = re.compile(r'''
             (                           # start capture group for a token 
               `[^`]+`(?:\([^)]*\))?      |   # backquoted identifier w/ optional parentheses
               \w+(?:\([^)]*\))?          |   # a word with optional parentheses
               '(?:\\'|[^'])*'           |   # a single-quoted string
               "(?:\\"|[^"])*"           |   # a double-quoted string
               [^\s]+                      # fallback: any sequence of non-whitespace characters
             )
             ''', re.VERBOSE)
        tokens = token_pattern.findall(sql_line)

        # Now, split the column definition into:
        #   token0 = column name,
        #   token1 = data type (which might be multiple tokens, e.g. DOUBLE PRECISION, INT UNSIGNED,
        #            or a word+parentheses like VARCHAR(254) or NUMERIC(5, 2)),
        #   remaining tokens: the parameters such as DEFAULT, NOT, etc.
        #
        # We define a set of keywords that indicate the start of column options.
        constraint_keywords = {
            "DEFAULT", "NOT", "NULL", "AUTO_INCREMENT", "PRIMARY", "UNIQUE",
            "COMMENT", "COLLATE", "REFERENCES", "ON", "CHECK", "CONSTRAINT",
            "AFTER", "BEFORE", "GENERATED", "VIRTUAL", "STORED", "FIRST",
            "ALWAYS", "AS", "IDENTITY", "INVISIBLE", "PERSISTED",
        }

        if not tokens:
            return tokens
        # The first token is always the column name.
        column_name = tokens[0]

        # Now "merge" tokens after the column name that belong to the type.
        # (For many types the type is written as a single token already –
        #  e.g. "VARCHAR(254)" or "NUMERIC(5, 2)", but for types like
        #  "DOUBLE PRECISION" or "INT UNSIGNED" the .split() would produce two tokens.)
        type_tokens = []
        i = 1
        while i < len(tokens) and tokens[i].upper() not in constraint_keywords:
            type_tokens.append(tokens[i])
            i += 1
        merged_type = " ".join(type_tokens) if type_tokens else ""

        # The remaining tokens are passed through unchanged.
        param_tokens = tokens[i:]

        # Result: [column name, merged type, all the rest]
        if merged_type:
            return [column_name, merged_type] + param_tokens
        else:
            return [column_name] + param_tokens

    def __convert_alter_table_add_column(self, db_name, table_name, tokens):
        tokens = self._tokenize_alter_query(' '.join(tokens))

        if len(tokens) < 2:
            raise Exception('wrong tokens count', tokens)

        column_after = None
        column_first = False
        if tokens[-2].lower() == 'after':
            column_after = strip_sql_name(tokens[-1])
            tokens = tokens[:-2]
            if len(tokens) < 2:
                raise Exception('wrong tokens count', tokens)
        elif tokens[-1].lower() == 'first':
            column_first = True

        column_name = strip_sql_name(tokens[0])
        column_type_mysql = tokens[1]
        column_type_mysql_parameters = ' '.join(tokens[2:])

        column_type_ch = self.convert_field_type(column_type_mysql, column_type_mysql_parameters)

        # update table structure
        if self.db_replicator:
            table_structure = self.db_replicator.state.tables_structure[table_name]
            mysql_table_structure: TableStructure = table_structure[0]
            ch_table_structure: TableStructure = table_structure[1]

            if column_first:
                mysql_table_structure.add_field_first(
                    TableField(name=column_name, field_type=column_type_mysql)
                )
                
                ch_table_structure.add_field_first(
                    TableField(name=column_name, field_type=column_type_ch)
                )
            else:
                if column_after is None:
                    column_after = strip_sql_name(mysql_table_structure.fields[-1].name)

                mysql_table_structure.add_field_after(
                    TableField(name=column_name, field_type=column_type_mysql),
                    column_after,
                )

                ch_table_structure.add_field_after(
                    TableField(name=column_name, field_type=column_type_ch),
                    column_after,
                )

        query = f'ALTER TABLE `{db_name}`.`{table_name}`