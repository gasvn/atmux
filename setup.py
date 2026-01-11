from setuptools import setup

setup(
    name='autotmux',
    version='0.3.1',
    py_modules=['autotmux'],
    entry_points={
        'console_scripts': [
            'atmux=autotmux:main',
        ],
    },
    author='Shanghua Gao',
    description='A tool to automatically list and attach to tmux sessions on Slurm nodes',
)
