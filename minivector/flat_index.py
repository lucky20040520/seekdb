"""Immutable disk Flat L2 index. Bounded streaming, no external index engine."""
import math
import os
import struct
import uuid
from . import segments

MAGIC = b'MVFLAT1\0'
HEADER = struct.Struct('<8sII')
LOCATION = struct.Struct('<qQ')
ALGORITHM = 'flat_l2_v1'


def layout(table, column):
    vector_column = next(i for i,c in enumerate(table['columns']) if c['name']==column)
    primary_column = next(i for i,c in enumerate(table['columns']) if c['primary'])
    dimension = table['columns'][vector_column]['size']
    return vector_column, primary_column, dimension


def valid(directory, row_file, index_file, table, column):
    if not index_file:
        return False
    path = directory/index_file
    vector_column, _, dimension = layout(table, column)
    try:
        with path.open('rb') as source:
            if source.read(HEADER.size) != HEADER.pack(MAGIC,dimension,vector_column):
                return False
        size = path.stat().st_size-HEADER.size
        width = LOCATION.size+4*dimension
        if size < 0 or size % width:
            return False
        count = segments.row_count(directory/row_file)
        if count is not None and size//width != count:
            return False
        return True
    except OSError:
        return False


def build(directory, row_file, table, column):
    vector_column, primary_column, dimension = layout(table,column)
    vector_format = struct.Struct('<%df'%dimension)
    filename = 'index-'+uuid.uuid4().hex+'.flat'
    target = directory/filename
    try:
        with segments.Reader(directory/row_file,table['columns']) as source, target.open('wb') as output:
            output.write(HEADER.pack(MAGIC,dimension,vector_column))
            for offset,row in source:
                output.write(LOCATION.pack(row[primary_column],offset))
                output.write(vector_format.pack(*row[vector_column]))
            output.flush()
            os.fsync(output.fileno())
        return filename
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def candidates(directory, table, descriptor, query, matches, filtered):
    vector_column, _, dimension = layout(table,descriptor['column'])
    record = struct.Struct('<qQ%df'%dimension)
    for row_file,index_file in descriptor['segments'].items():
        # Record locations always refer to the same immutable data generation.
        with (directory/index_file).open('rb') as index, segments.Reader(directory/row_file,table['columns']) as rows:
            if index.read(HEADER.size) != HEADER.pack(MAGIC,dimension,vector_column):
                raise OSError('flat index header mismatch')
            while True:
                block = index.read(record.size)
                if not block:
                    break
                if len(block) != record.size:
                    raise OSError('truncated flat index record')
                values = record.unpack(block)
                primary,offset = values[:2]
                if filtered:
                    if not matches(rows.at(offset)):
                        continue
                distance = math.sqrt(math.fsum((a-b)**2 for a,b in zip(values[2:],query)))
                yield [distance,primary,row_file,offset]


def read_row(directory, location, table):
    with segments.Reader(directory/location[2],table['columns']) as source:
        return source.at(location[3])
