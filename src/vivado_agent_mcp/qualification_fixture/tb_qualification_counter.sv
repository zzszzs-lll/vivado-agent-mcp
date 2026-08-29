`timescale 1ns/1ps

module tb_qualification_counter;
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic [3:0] count;
    logic finished = 1'b0;
    int expected = 0;

    qualification_counter dut (
        .clk(clk),
        .rst_n(rst_n),
        .count(count)
    );

    always #5 clk = ~clk;

    initial begin
        repeat (3) @(posedge clk);
        @(negedge clk);
        rst_n = 1'b1;
        repeat (12) begin
            @(posedge clk);
            #1;
            expected = (expected + 1) & 4'hf;
            if (count !== expected[3:0]) begin
                finished = 1'b1;
                $fatal(1, "TB_FAIL expected=%0d got=%0d", expected, count);
            end
        end
        finished = 1'b1;
        $display("TB_PASS count=%0d", count);
        $finish;
    end

    initial begin
        #2000;
        if (!finished) begin
            $fatal(1, "TB_FAIL timeout");
        end
        $finish;
    end
endmodule
