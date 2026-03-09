#include <stdio.h>

int main(int argc, char **argv) {
    FILE *in, *out;
    int c, count;

    if (argc < 4) {
        fprintf(stderr, "Usage: %s e|u input output\n", argv[0]);
        return 1;
    }

    in  = fopen(argv[2], "r");
    out = fopen(argv[3], "w");
    if (!in || !out) {
        perror("File error");
        return 1;
    }

    if (*argv[1] == 'e') {
        while ((c = fgetc(in)) != EOF) {
            count = c;
            for (int i = 0; i < count; i++)
                fputc('_', out);
            fputc('-', out);
        }

    } else if (*argv[1] == 'u') {
        count = 0;
        while ((c = fgetc(in)) != EOF) {
            if (c == '_') {
                count++;
            } else if (c == '-') {
                fputc(count, out);
                count = 0;
            }
        }
        if (count > 0)
            fputc(count, out);

    } else {
        fprintf(stderr, "Invalid mode. Use 'e' for encode or 'u' for unencode.\n");
        return 1;
    }

    fclose(in);
    fclose(out);
    return 0;
}
