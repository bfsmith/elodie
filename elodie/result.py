from tabulate import tabulate


class Result(object):

    def __init__(self):
        self.records = []
        self.success = 0
        self.error = 0
        self.error_items = []
        self.duplicate = 0
        self.duplicate_items = []

    def append(self, row):
        id, status = row

        # status can only be True, False, or None
        if status is True:
            self.success += 1
        elif status is None: # status is only ever None if file checksum matched an existing file checksum and is therefore a duplicate file
            self.duplicate += 1
            self.duplicate_items.append(id)
        else:
            self.error += 1
            self.error_items.append(id)

    def write(self, duration_seconds=None):
        print("\n")
        headers = ["Metric", "Count"]
        result = [
            ["Success", self.success],
            ["Error", self.error],
            ["Duplicate, not imported", self.duplicate],
        ]
        if duration_seconds is not None:
            result.append(["Time elapsed", self._format_duration(duration_seconds)])
            if duration_seconds > 0:
                total_files = self.success + self.error + self.duplicate
                rate = total_files / (duration_seconds / 60.0)
                result.append(["Processing rate", "{:.1f} files / minute".format(rate)])

        print("****** SUMMARY ******")
        print(tabulate(result, headers=headers))

    def _format_duration(self, seconds):
        s = int(round(seconds))
        if s < 60:
            return "{} seconds".format(s)
        elif s < 3600:
            m, sec = divmod(s, 60)
            return "{} minutes {} seconds".format(m, sec)
        else:
            h, r = divmod(s, 3600)
            m, sec = divmod(r, 60)
            return "{} hours {} minutes {} seconds".format(h, m, sec)
