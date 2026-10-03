; fib.asm — 迭代求 Fibonacci(20), 演示算术与循环
; 输出: Fib(20) = 6765

        MOV r0, msg
        CALL print_str        ; 打印 "Fib(20) = "

        ; a = 0, b = 1, i = 2
        MOV r0, 0             ; a
        MOV r1, 1             ; b
        MOV r2, 2             ; i

_loop:
        CMPI r2, 21
        JGE _done             ; i >= 21 结束
        MOV r3, r1            ; t = b
        ADD r1, r0, r1        ; b = a + b
        MOV r0, r3            ; a = t
        MOV r3, 1
        ADD r2, r2, r3        ; i++
        JMP _loop

_done:
        OUTI r1               ; 打印结果 (b = F(20))
        MOV r0, 10
        OUT r0                ; 换行
        MOV r0, 0
        HLT

print_str:
_ploop:
        LDB r1, [r0]
        CMPI r1, 0
        JE _pdone
        OUT r1
        MOV r1, 1
        ADD r0, r0, r1
        JMP _ploop
_pdone:
        RET

msg:
        DB "Fib(20) = "
        DB 0
