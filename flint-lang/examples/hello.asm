; hello.asm — 第一个燧石汇编程序
; 输出 "Hello, World!" 后换行, 退出码 0

        MOV r0, msg          ; r0 = 字符串地址
        CALL print_str
        MOV r0, 0            ; 退出码 0
        HLT

print_str:
        ; 循环打印字符串直到 NUL
_loop:
        LDB r1, [r0]         ; 取当前字符
        CMPI r1, 0
        JE _done
        OUT r1               ; 输出字符
        MOV r1, 1
        ADD r0, r0, r1
        JMP _loop
_done:
        RET

        ; ---------- 数据段 ----------
msg:
        DB "Hello, World!"
        DB 10                ; 换行
        DB 0
