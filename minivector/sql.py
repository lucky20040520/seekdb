"""Provisional SQL subset; official grammar and error codes remain unprovided."""
import re
from .engine import fail


TOKEN = re.compile(r"\s+|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:''|[^'])*'|`(?:``|[^`])*`|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?|[A-Za-z_][A-Za-z_0-9]*|<=|>=|[(),;*\[\]=<>+\-]")


class Parser:
    def __init__(self, sql):
        self.tokens = []
        position = 0
        while position < len(sql):
            match = TOKEN.match(sql, position)
            if not match:
                fail("unrecognized SQL token", "SYNTAX")
            token = match.group()
            position = match.end()
            if not token.isspace() and not token.startswith(("--", "/*")):
                self.tokens.append(token)
        if self.tokens and self.tokens[-1] == ";":
            self.tokens.pop()
        self.position = 0

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else ""

    def take(self):
        token = self.peek()
        if not token:
            fail("unexpected end of statement", "SYNTAX")
        self.position += 1
        return token

    def eat(self, token):
        if self.peek().upper() == token:
            self.position += 1
            return True
        return False

    def expect(self, token):
        if not self.eat(token):
            fail("expected " + token, "SYNTAX")

    def identifier(self):
        token = self.take()
        if token.startswith("`"):
            return token[1:-1].replace("``", "`").lower()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", token):
            fail("expected identifier", "SYNTAX")
        return token.lower()

    def value(self):
        if self.eat("["):
            values = []
            if not self.eat("]"):
                while True:
                    values.append(self.value())
                    if self.eat("]"):
                        break
                    self.expect(",")
            return values
        negative = self.eat("-")
        positive = self.eat("+") if not negative else False
        sign = -1 if negative else 1
        token = self.take()
        if token.startswith("'"):
            if negative or positive:
                fail("invalid string sign", "SYNTAX")
            return token[1:-1].replace("''", "'")
        if token.upper() in ("NAN", "INFINITY", "INF"):
            return sign * float(token)
        try:
            return sign * (float(token) if any(c in token.lower() for c in ".e") else int(token))
        except ValueError:
            fail("expected literal", "SYNTAX")

    def expression(self):
        if self.eat("L2_DISTANCE"):
            self.expect("(")
            column = self.identifier()
            self.expect(",")
            vector = self.value()
            self.expect(")")
            return ("distance", column, vector)
        return ("column", self.identifier())

    def where(self):
        conditions = []
        if self.eat("WHERE"):
            while True:
                column = self.identifier()
                operator = self.take()
                if operator not in ("=", "<", "<=", ">", ">="):
                    fail("unsupported comparison", "SYNTAX")
                conditions.append((column, operator, self.value()))
                if not self.eat("AND"):
                    break
        return conditions

    def finish(self):
        if self.peek():
            fail("unsupported or trailing SQL: " + self.peek(), "SYNTAX")

    def parse(self):
        verb = self.take().upper()
        if verb in ("BEGIN", "COMMIT", "ROLLBACK"):
            self.finish()
            return (verb.lower(), [])
        if verb == "CREATE":
            if self.eat("VECTOR"):
                self.expect("INDEX")
                index = self.identifier()
                self.expect("ON")
                table = self.identifier()
                self.expect("(")
                column = self.identifier()
                self.expect(")")
                self.finish()
                return ("create_index", [index, table, column])
            self.expect("TABLE")
            table = self.identifier()
            self.expect("(")
            columns, table_pk = [], None
            while True:
                if self.eat("PRIMARY"):
                    self.expect("KEY")
                    self.expect("(")
                    if table_pk is not None:
                        fail("multiple primary key declarations", "SCHEMA")
                    table_pk = self.identifier()
                    self.expect(")")
                else:
                    name, kind = self.identifier(), self.take().upper()
                    size = None
                    if kind in ("VARCHAR", "VECTOR"):
                        self.expect("(")
                        size = self.value()
                        if type(size) is not int:
                            fail("integer type size required", "SYNTAX")
                        self.expect(")")
                    primary = self.eat("PRIMARY")
                    if primary:
                        self.expect("KEY")
                    columns.append(dict(name=name, kind=kind, size=size, primary=primary))
                if self.eat(")"):
                    break
                self.expect(",")
            if table_pk is not None:
                if any(c["primary"] for c in columns) or not any(c["name"] == table_pk for c in columns):
                    fail("invalid primary key declaration", "SCHEMA")
                for column in columns:
                    column["primary"] = column["name"] == table_pk
            self.finish()
            return ("create", [table, columns])
        if verb == "INSERT":
            self.expect("INTO")
            table, columns = self.identifier(), None
            if self.eat("("):
                columns = [self.identifier()]
                while self.eat(","):
                    columns.append(self.identifier())
                self.expect(")")
            self.expect("VALUES")
            rows = []
            while True:
                self.expect("(")
                row = [self.value()]
                while self.eat(","):
                    row.append(self.value())
                self.expect(")")
                rows.append(row)
                if not self.eat(","):
                    break
            self.finish()
            return ("insert", [table, rows, columns])
        if verb == "SELECT":
            if self.eat("*"):
                projections = "*"
            else:
                projections = []
                while True:
                    expr = self.expression()
                    alias = self.identifier() if self.eat("AS") else (expr[1] if expr[0] == "column" else "l2_distance")
                    projections.append((expr, alias))
                    if not self.eat(","):
                        break
            self.expect("FROM")
            table = self.identifier()
            conditions = self.where()
            order = None
            if self.eat("ORDER"):
                self.expect("BY")
                expr = self.expression()
                descending = self.eat("DESC")
                if not descending:
                    self.eat("ASC")
                order = (expr, descending)
            limit = self.value() if self.eat("LIMIT") else None
            self.finish()
            return ("select", [table, projections, conditions, order, limit])
        if verb == "UPDATE":
            table = self.identifier()
            self.expect("SET")
            assignments = []
            while True:
                column = self.identifier()
                self.expect("=")
                assignments.append((column, self.value()))
                if not self.eat(","):
                    break
            conditions = self.where()
            self.finish()
            return ("modify", [table, conditions, assignments])
        if verb == "DELETE":
            self.expect("FROM")
            table = self.identifier()
            conditions = self.where()
            self.finish()
            return ("modify", [table, conditions])
        fail("unsupported statement", "SYNTAX")


def execute(database, sql, mode="exact"):
    method, args = Parser(sql).parse()
    if method == "select":
        return database.select(*args, mode=mode)
    result = getattr(database, method)(*args)
    return result
