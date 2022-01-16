apt-get -qq update && apt-get -qq install time
aws s3 cp s3://hummingbird-fastq-novaseq-wgs-pcr-free/downsampled_test/HG001.novaseq.pcr-free.30x.R1_seqtk_361142.fastq HG001.novaseq.pcr-free.30x.R1_seqtk_361142.fastq
aws s3 cp s3://hummingbird-fastq-novaseq-wgs-pcr-free/downsampled_test/HG001.novaseq.pcr-free.30x.R2_seqtk_361142.fastq HG001.novaseq.pcr-free.30x.R2_seqtk_361142.fastq
aws s3 cp s3://hummingbird-fastq-novaseq-wgs-pcr-free/Reference_GRCh37 Reference_GRCh37 --recursive
/usr/bin/time -a -f "%e %M" -o result.txt bwa mem -t 2 -M -R '@RG\tID:0\tLB:Library\tPL:Illumina\tSM:' Reference_GRCh37/GRCh37-lite.fa HG001.novaseq.pcr-free.30x.R1_seqtk_361142.fastq HG001.novaseq.pcr-free.30x.R2_seqtk_361142.fastq > output.sam
awk '{print $1}' result.txt > time_result.txt
awk '{print $2}' result.txt > mem_result.txt
aws s3 cp time_result.txt s3://hummingbird-fastq-novaseq-wgs-pcr-free/result_test/bwa/361142/i3.large/time/try$AWS_BATCH_JOB_ARRAY_INDEX.txt
aws s3 cp mem_result.txt s3://hummingbird-fastq-novaseq-wgs-pcr-free/result_test/bwa/361142/i3.large/mem/try$AWS_BATCH_JOB_ARRAY_INDEX.txt
aws s3 cp output.sam s3://hummingbird-fastq-novaseq-wgs-pcr-free/output_test/bwa/output_2_361142.sam


