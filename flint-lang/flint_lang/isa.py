# -*- coding: utf-8 -*-
"""
flint_lang.isa — 燧石语言(Flint)指令集架构定义与指令编码。

架构概览
--------
* 字长: 32 位, 小端序
* 内存: 65536 字节(64 KB), 字节寻址
* 寄存器: R0~R15 通用寄存器(R15 约定为栈指针 SP), PC、FLAGS 为特殊寄存器
* 标志位: Z(零) N(负) C(进位/无符号小于) O(溢出)

指令编码(均为 4 字节, 唯 MOV/LDA 立即数为 8 字节):
  R 型: op(8) rd(4) rs1(4) rs2(4) pad(12)        —— 三寄存器运算
  I 型: op(8) rd(4) rs1(4) imm16(16)             —— 寄存器 + 16 位立即数
  B 型: op(8) off24(24)                          —— 相对跳转偏移
  J 型: op(8) rd(4) pad(4) imm32(32) [第二字]     —— 32 位立即数
"""

MEM_SIZE = 0x10000          # 64 KB
NUM_REGS = 16
SP_REG = 15                 # R15 即栈指针

# 标志位
F_Z = 1 << 0
F_N = 1 << 1
F_C = 1 << 2
F_O = 1 << 3

# ---------- 操作码表 ----------
OPCODES = {
    "MOVR": 0x01,  # MOV rd, rs1         (R 型)
    "MOVI": 0x02,  # MOV rd, imm32       (J 型)
    "ADD": 0x03,   # ADD rd, rs1, rs2    rd = rs1 + rs2
    "SUB": 0x04,   # SUB rd, rs1, rs2    rd = rs1 - rs2
    "MUL": 0x05,   # MUL rd, rs1, rs2    rd = rs1 * rs2 (低 32 位)
    "DIV": 0x06,   # DIV rd, rs1, rs2    rd = rs1 / rs2 (向零截断)
    "MOD": 0x07,   # MOD rd, rs1, rs2    rd = rs1 % rs2
    "AND": 0x08,   # AND rd, rs1, rs2
    "OR":  0x09,   # OR  rd, rs1, rs2
    "XOR": 0x0A,   # XOR rd, rs1, rs2
    "NOT": 0x0B,   # NOT rd, rs1        rd = ~rs1
    "SHL": 0x0C,   # SHL rd, rs1, rs2   rd = rs1 << (rs2 & 31)
    "SHR": 0x0D,   # SHR rd, rs1, rs2   rd = rs1 >> (rs2 & 31) 逻辑右移
    "CMP": 0x0E,   # CMP rs1, rs2       仅更新标志位
    "JMP": 0x0F,   # JMP off24
    "JE":  0x10,   # 相等则跳
    "JNE": 0x11,
    "JG":  0x12,   # 有符号大于
    "JL":  0x13,   # 有符号小于
    "JGE": 0x14,
    "JLE": 0x15,
    "CALL": 0x16,  # 压入返回地址并跳转
    "RET": 0x17,
    "PUSH": 0x18,  # PUSH rs1
    "POP": 0x19,   # POP  rd
    "LD":  0x1A,   # LD  rd, [rs1 + imm16]   载入 32 位字
    "ST":  0x1B,   # ST  [rs1 + imm16], rd   存储 32 位字
    "LDB": 0x1C,   # LDB rd, [rs1 + imm16]   载入字节(零扩展)
    "STB": 0x1D,   # STB [rs1 + imm16], rd   存储低字节
    "LDA": 0x1E,   # LDA rd, label           rd = 标签地址 (J 型)
    "HLT": 0x1F,
    "IN":  0x20,   # IN  rd                读入一个字节(EOF 时为 0)
    "OUT": 0x21,   # OUT rs1               输出低字节
    "OUTI": 0x22,  # OUTI rs1              输出十进制有符号整数
    "CMPI": 0x23,  # CMPI rs1, imm16       与 16 位立即数比较, 仅更新标志位
    "TRAP": 0x24,  # TRAP imm16            触发运行时陷阱(越界等), 停机并报错
}

OP_TO_NAME = {v: k for k, v in OPCODES.items()}

# 双字(J 型)指令
J_TYPE_OPS = {"MOVI", "LDA"}

# R 型指令的操作数字段布局
R_TYPE_OPS = {"ADD", "SUB", "MUL", "DIV", "MOD", "AND", "OR", "XOR", "SHL", "SHR"}


# ---------- 数值工具 ----------
def to_u32(x: int) -> int:
    return x & 0xFFFFFFFF


def to_i32(x: int) -> int:
    x = x & 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value & ((1 << bits) - 1)) - (1 << bits) if value & sign else value


def fmt_u32(x: int) -> bytes:
    return x.to_bytes(4, "little", signed=False)


def fmt_i32(x: int) -> bytes:
    return to_u32(x).to_bytes(4, "little")


def parse_u32(b: bytes, off: int = 0) -> int:
    return int.from_bytes(b[off:off + 4], "little")


# ---------- 指令编码 ----------
def enc_r(op: str, rd: int, rs1: int, rs2: int) -> bytes:
    """R 型: op(8) rd(4) rs1(4) rs2(4) pad(12)"""
    word = (OPCODES[op] << 24) | ((rd & 0xF) << 20) | ((rs1 & 0xF) << 16) | ((rs2 & 0xF) << 12)
    return fmt_u32(word)


def enc_i(op: str, rd: int, rs1: int, imm16: int) -> bytes:
    """I 型: op(8) rd(4) rs1(4) imm16(16)"""
    word = (OPCODES[op] << 24) | ((rd & 0xF) << 20) | ((rs1 & 0xF) << 16) | (imm16 & 0xFFFF)
    return fmt_u32(word)


def enc_b(op: str, off24: int) -> bytes:
    """B 型: op(8) off24(24) 有符号偏移"""
    word = (OPCODES[op] << 24) | (off24 & 0xFFFFFF)
    return fmt_u32(word)


def enc_j(op: str, rd: int, imm32: int) -> bytes:
    """J 型: 第一字 op(8) rd(4) pad(20), 第二字 imm32 —— 共 8 字节"""
    word = (OPCODES[op] << 24) | ((rd & 0xF) << 20)
    return fmt_u32(word) + fmt_u32(imm32)


# ---------- 指令解码(供反汇编与虚拟机使用) ----------
def instr_size(word: int) -> int:
    op = (word >> 24) & 0xFF
    name = OP_TO_NAME.get(op)
    return 8 if name in J_TYPE_OPS else 4


def disasm(bytecode: bytes, addr: int) -> str:
    """将 addr 处的一条指令反汇编为可读文本。"""
    word = parse_u32(bytecode, addr)
    op = (word >> 24) & 0xFF
    name = OP_TO_NAME.get(op, "???")
    rd = (word >> 20) & 0xF
    rs1 = (word >> 16) & 0xF
    rs2 = (word >> 12) & 0xF
    imm16 = sign_extend(word & 0xFFFF, 16)
    off24 = sign_extend(word & 0xFFFFFF, 24)
    rn = lambda r: f"r{r}"

    if name in ("MOVR", "MOVI", "LDA"):
        if name == "MOVR":
            return f"MOV {rn(rd)}, {rn(rs1)}"
        imm32 = parse_u32(bytecode, addr + 4)
        return f"{'LDA' if name == 'LDA' else 'MOV'} {rn(rd)}, {imm32 if name == 'MOVI' else hex(imm32)}"
    if name in R_TYPE_OPS:
        return f"{name} {rn(rd)}, {rn(rs1)}, {rn(rs2)}"
    if name == "NOT":
        return f"NOT {rn(rd)}, {rn(rs1)}"
    if name == "CMP":
        return f"CMP {rn(rd)}, {rn(rs2)}"
    if name == "CMPI":
        return f"CMPI {rn(rd)}, {imm16}"
    if name == "TRAP":
        return f"TRAP {imm16}"
    if name in ("JMP", "JE", "JNE", "JG", "JL", "JGE", "JLE", "CALL"):
        return f"{name} {addr + 4 + off24:#08x}"
    if name == "RET":
        return "RET"
    if name in ("PUSH", "OUT", "OUTI"):
        return f"{name} {rn(rd)}"
    if name == "POP":
        return f"POP {rn(rd)}"
    if name == "IN":
        return f"IN {rn(rd)}"
    if name == "HLT":
        return "HLT"
    if name == "LD":
        return f"LD {rn(rd)}, [r{rs1}{('+' + str(imm16)) if imm16 else ''}]"
    if name == "ST":
        return f"ST [r{rs1}{('+' + str(imm16)) if imm16 else ''}], {rn(rd)}"
    if name == "LDB":
        return f"LDB {rn(rd)}, [r{rs1}{('+' + str(imm16)) if imm16 else ''}]"
    if name == "STB":
        return f"STB [r{rs1}{('+' + str(imm16)) if imm16 else ''}], {rn(rd)}"
    return f"??? op=0x{op:02x}"
