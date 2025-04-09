from setuptools import setup, find_packages
import os

# Function to read the requirements.txt file
def read_requirements():
    requirements_path = os.path.join(os.path.dirname(__file__), 'requirements.txt')
    if not os.path.exists(requirements_path):
        print("Warning: requirements.txt not found. Proceeding without dependencies.")
        return []
    with open(requirements_path, 'r') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith('#')]

# Basic metadata
setup(
    name='arch-cleaner',
    version='0.1.0', # You can update this version number as needed
    author='Cline (AI Assistant)', # Or replace with your name/handle
    description='Arch Linux AI Storage Agent - Manage and optimize system storage.',
    long_description=open('README.md').read() if os.path.exists('README.md') else '', # Optional: Use README
    long_description_content_type='text/markdown', # Optional
    url='<your_repository_url_here>', # Optional: Add your repo URL if you have one
    packages=find_packages(), # Automatically find packages like 'arch_cleaner'
    install_requires=read_requirements(), # Read dependencies from requirements.txt
    python_requires='>=3.8', # Specify minimum Python version
    entry_points={
        'console_scripts': [
            'arch-cleaner = main:main', # This creates the 'arch-cleaner' command
        ],
    },
    classifiers=[ # Optional metadata
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License', # Choose an appropriate license
        'Operating System :: POSIX :: Linux',
        'Topic :: System :: Filesystems',
        'Topic :: System :: Archiving :: Backup',
        'Topic :: Utilities',
    ],
)
