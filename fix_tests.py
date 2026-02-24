import re
import sys

def main():
    filepaths = sys.argv[1:]
    for filepath in filepaths:
        with open(filepath, "r") as f:
            content = f.read()

        # Fix handle_feature_ideation_request args
        content = re.sub(
            r'handlers\.handle_feature_ideation_request\(\s*update=update,\s*context=context,\s*status_msg=status_msg',
            r'handlers.handle_feature_ideation_request(ctx=ctx, status_msg_id="42"',
            content,
            flags=re.MULTILINE
        )

        # Fix feature_callback_handler args
        content = re.sub(
            r'handlers\.feature_callback_handler\(\s*update=update,\s*context=context,\s*deps=deps(,\s*match=None)?\)',
            r'handlers.feature_callback_handler(ctx=ctx, deps=deps)',
            content,
            flags=re.MULTILINE
        )
        content = re.sub(
            r'handlers\.feature_callback_handler\(\s*update=update,\s*context=context,\s*deps=deps\s*\)',
            r'handlers.feature_callback_handler(ctx=ctx, deps=deps)',
            content,
            flags=re.MULTILINE
        )

        with open(filepath, "w") as f:
            f.write(content)

if __name__ == "__main__":
    main()
